import { useEffect, useRef, useState } from "react";

import type { AgentCandidateImage } from "../components/MultiCandidateCard";

import type { AgentToolEvent } from "./agentTypes";
import { api } from "./api";
import { useI18n } from "./preferences";
import type { ImageSessionDetail, ImageSessionGenerationTask } from "./types";

/** 有界轮询参数：每 3 秒查一次 image-session 详情，最多 40 次（约 120 秒）后放弃。 */
export const CANDIDATE_POLL_INTERVAL_MS = 3000;
export const CANDIDATE_POLL_MAX_ATTEMPTS = 40;

/** pending 态 tool_result 中可用于轮询的 image_session_id（缺省或空串返回 null）。 */
export function pendingImageSessionId(result: AgentToolEvent["result"]): string | null {
  if (result.pending !== true) return null;
  const id = result.image_session_id;
  return typeof id === "string" && id !== "" ? id : null;
}

/** 从流式 tool_result 里收集需要轮询的 image_session_id（仅 generate_image，去重保序）。 */
export function collectPendingImageSessionIds(events: AgentToolEvent[]): string[] {
  const ids: string[] = [];
  for (const event of events) {
    if (event.tool !== "generate_image") continue;
    const id = pendingImageSessionId(event.result);
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids;
}

/** pending 事件为该 image_session 声明的预期候选张数（取最大值；未知返回 null）。 */
export function expectedCandidatesForSession(
  events: AgentToolEvent[],
  imageSessionId: string,
): number | null {
  let expected: number | null = null;
  for (const event of events) {
    if (event.tool !== "generate_image" || pendingImageSessionId(event.result) !== imageSessionId) {
      continue;
    }
    const value = event.result.expected_candidates;
    if (typeof value === "number" && Number.isFinite(value) && value > 0) {
      expected = expected == null ? value : Math.max(expected, value);
    }
  }
  return expected;
}

function latestGenerationTask(detail: ImageSessionDetail | null): ImageSessionGenerationTask | null {
  const tasks = [...(detail?.generation_tasks ?? [])].sort(
    (a, b) =>
      String(a.created_at ?? "").localeCompare(String(b.created_at ?? "")) ||
      String(a.id ?? "").localeCompare(String(b.id ?? "")),
  );
  return tasks.at(-1) ?? null;
}

/**
 * 把 image-session 详情映射为候选卡数据（与后端 `_generation_candidates` 契约一致）：
 * 取最新生成任务的 result_generation_group_id 分组下已有成图的轮次，按 candidate_index 排序。
 * 最新任务还没产出分组号（首张候选未完成）时返回空数组，表示继续等待。
 */
export function candidatesFromImageSessionDetail(
  detail: ImageSessionDetail,
  formatLabel: (index: number) => string,
): AgentCandidateImage[] {
  const groupId = latestGenerationTask(detail)?.result_generation_group_id ?? null;
  if (!groupId) return [];
  const rounds = (detail?.rounds ?? []).filter(
    (round) => round.generation_group_id === groupId && Boolean(round.generated_asset),
  );
  rounds.sort((a, b) => a.candidate_index - b.candidate_index);
  return rounds.map((round) => ({
    asset_id: round.generated_asset.id,
    url: round.generated_asset.download_url,
    preview_url: round.generated_asset.preview_url,
    label: formatLabel(round.candidate_index),
  }));
}

/** 最新生成任务是否明确失败（status === "failed"）。网络错误与取消不算失败，仍走等待或超时兜底。 */
export function hasFailedGenerationTask(detail: ImageSessionDetail | null): boolean {
  return latestGenerationTask(detail)?.status === "failed";
}

/** 单个 image_session 的轮询展示状态。 */
export interface CandidatePollState {
  candidates: AgentCandidateImage[];
  elapsedSeconds: number;
  expired: boolean;
  /** 最新生成任务明确失败（状态可查时），pending 卡切换为失败态。 */
  failed: boolean;
}

export type CandidatePollUpdate = {
  candidates?: AgentCandidateImage[];
  elapsedSeconds?: number;
  expired?: boolean;
  failed?: boolean;
};

export const EMPTY_CANDIDATE_POLL_STATE: CandidatePollState = {
  candidates: [],
  elapsedSeconds: 0,
  expired: false,
  failed: false,
};

/** 把轮询回调合并进按 image_session_id 索引的状态表（纯函数）。 */
export function mergeCandidatePollUpdate(
  prev: Record<string, CandidatePollState>,
  imageSessionId: string,
  update: CandidatePollUpdate,
): Record<string, CandidatePollState> {
  const current = prev[imageSessionId] ?? EMPTY_CANDIDATE_POLL_STATE;
  return {
    ...prev,
    [imageSessionId]: {
      candidates: update.candidates ?? current.candidates,
      elapsedSeconds: update.elapsedSeconds ?? current.elapsedSeconds,
      expired: current.expired || update.expired === true,
      failed: current.failed || update.failed === true,
    },
  };
}

export interface CandidatePollerOptions {
  imageSessionId: string;
  fetchDetail: (imageSessionId: string) => Promise<ImageSessionDetail>;
  formatLabel: (index: number) => string;
  onUpdate: (update: CandidatePollUpdate) => void;
  /** 预期候选张数：已知时凑齐才算就绪；未知时任一候选就绪即回调。 */
  expectedCandidates?: number | null;
  intervalMs?: number;
  maxAttempts?: number;
}

export interface CandidatePoller {
  start(): void;
  dispose(): void;
}

/**
 * 有界轮询器：立即发起首次查询，之后按 interval 重试；凑齐候选、达到次数上限或 dispose 后停止。
 * 达到上限时若已有部分候选就先交出部分候选，否则回调 expired。
 */
export function createCandidatePoller(options: CandidatePollerOptions): CandidatePoller {
  const intervalMs = options.intervalMs ?? CANDIDATE_POLL_INTERVAL_MS;
  const maxAttempts = options.maxAttempts ?? CANDIDATE_POLL_MAX_ATTEMPTS;
  const expectedCandidates = options.expectedCandidates ?? null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempts = 0;
  let inFlight = false;
  let finished = false;
  let disposed = false;
  let partial: AgentCandidateImage[] = [];

  const isReady = (candidates: AgentCandidateImage[]): boolean =>
    expectedCandidates == null ? candidates.length > 0 : candidates.length >= expectedCandidates;

  const tick = async (): Promise<void> => {
    if (disposed || finished || inFlight) return;
    inFlight = true;
    attempts += 1;
    // 单次查询失败按未就绪处理（回落 null），直到达到上限
    const detail = await options.fetchDetail(options.imageSessionId).catch(() => null);
    inFlight = false;
    if (disposed) return;
    let candidates: AgentCandidateImage[] = [];
    if (detail !== null) {
      candidates = candidatesFromImageSessionDetail(detail, options.formatLabel);
    }
    if (candidates.length > 0) {
      partial = candidates;
      if (isReady(candidates)) {
        finished = true;
        options.onUpdate({ candidates });
        return;
      }
    }
    // 生成任务明确失败：停止轮询并上报失败态（文案在卡片层，不透出技术细节）
    if (hasFailedGenerationTask(detail)) {
      finished = true;
      options.onUpdate({ failed: true });
      return;
    }
    if (attempts >= maxAttempts) {
      finished = true;
      options.onUpdate(partial.length > 0 ? { candidates: partial } : { expired: true });
      return;
    }
    timer = setTimeout(() => {
      timer = null;
      if (disposed || finished) return;
      options.onUpdate({ elapsedSeconds: attempts * Math.round(intervalMs / 1000) });
      void tick();
    }, intervalMs);
  };

  return {
    start() {
      if (disposed || finished || timer || inFlight) return;
      void tick();
    },
    dispose() {
      disposed = true;
      finished = true;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}

/**
 * 工作台候选轮询 hook：为每条 pending 的 generate_image tool_result 按 image_session_id 去重调度
 * 一个有界轮询器（同一会话多个候选卡共享同一份结果），流结束/会话切换/卸载时清理全部定时器。
 */
export function useCandidatePolls(events: AgentToolEvent[]): Record<string, CandidatePollState> {
  const { t } = useI18n();
  const [pollStates, setPollStates] = useState<Record<string, CandidatePollState>>({});
  const pollersRef = useRef<Map<string, CandidatePoller>>(new Map());
  const formatLabelRef = useRef<(index: number) => string>(() => "");
  formatLabelRef.current = (index: number) => t("workbench.candidate.label", { index });

  const pendingIds = collectPendingImageSessionIds(events);
  const pendingKey = pendingIds.join("|");

  useEffect(() => {
    const map = pollersRef.current;
    for (const [imageSessionId, poller] of map) {
      if (!pendingIds.includes(imageSessionId)) {
        poller.dispose();
        map.delete(imageSessionId);
      }
    }
    for (const imageSessionId of pendingIds) {
      if (map.has(imageSessionId)) continue;
      setPollStates((prev) => ({ ...prev, [imageSessionId]: EMPTY_CANDIDATE_POLL_STATE }));
      const poller = createCandidatePoller({
        imageSessionId,
        fetchDetail: (id) => api.getImageSession(id),
        formatLabel: (index) => formatLabelRef.current(index),
        expectedCandidates: expectedCandidatesForSession(events, imageSessionId),
        onUpdate: (update) =>
          setPollStates((prev) => mergeCandidatePollUpdate(prev, imageSessionId, update)),
      });
      map.set(imageSessionId, poller);
      poller.start();
    }
    // 依赖收敛为 pendingKey：每条流式消息都会产生新的 events 数组，按 pending 集合变化调度即可
  }, [pendingKey]);

  useEffect(
    () => () => {
      for (const poller of pollersRef.current.values()) poller.dispose();
      pollersRef.current.clear();
    },
    [],
  );

  return pollStates;
}
