import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AgentToolEvent } from "./agentTypes";
import {
  CANDIDATE_POLL_INTERVAL_MS,
  CANDIDATE_POLL_MAX_ATTEMPTS,
  EMPTY_CANDIDATE_POLL_STATE,
  candidatesFromImageSessionDetail,
  collectPendingImageSessionIds,
  createCandidatePoller,
  expectedCandidatesForSession,
  mergeCandidatePollUpdate,
  type CandidatePollUpdate,
} from "./candidatePolling";
import type { ImageSessionDetail } from "./types";

function pendingResult(imageSessionId: string, expected = 2): AgentToolEvent["result"] {
  return {
    candidates: [],
    pending: true,
    expected_candidates: expected,
    image_session_id: imageSessionId,
  };
}

function toolEvent(tool: string, result: AgentToolEvent["result"]): AgentToolEvent {
  return { tool, result };
}

/** 只构造被测函数读取的字段，其余字段与映射无关。 */
function detailWith(
  tasks: Array<{ created_at: string; result_generation_group_id: string | null }>,
  rounds: Array<{
    generation_group_id: string | null;
    candidate_index: number;
    asset_id: string | null;
    created_at: string;
  }>,
): ImageSessionDetail {
  return {
    generation_tasks: tasks.map((task, index) => ({
      id: `task-${index}`,
      created_at: task.created_at,
      result_generation_group_id: task.result_generation_group_id,
    })),
    rounds: rounds.map((round) => ({
      id: `round-${round.generation_group_id}-${round.candidate_index}`,
      generation_group_id: round.generation_group_id,
      candidate_index: round.candidate_index,
      created_at: round.created_at,
      generated_asset: round.asset_id
        ? {
            id: round.asset_id,
            download_url: `/api/image-session-assets/${round.asset_id}/download`,
            preview_url: `/api/image-session-assets/${round.asset_id}/download?variant=preview`,
          }
        : null,
    })),
  } as unknown as ImageSessionDetail;
}

const formatLabel = (index: number) => `候选 ${index}`;

describe("collectPendingImageSessionIds", () => {
  it("收集 pending 且带 image_session_id 的 generate_image 事件，去重保序", () => {
    const events: AgentToolEvent[] = [
      toolEvent("generate_image", pendingResult("s1")),
      toolEvent("generate_image", pendingResult("s1")),
      toolEvent("generate_image", pendingResult("s2", 3)),
      toolEvent("generate_image", { pending: true }),
      toolEvent("generate_image", {
        candidates: [{ asset_id: "a", url: "u", label: "l" }],
      }),
      toolEvent("write_copy", { pending: true, image_session_id: "s3" }),
      toolEvent("generate_image", {}),
    ];
    expect(collectPendingImageSessionIds(events)).toEqual(["s1", "s2"]);
    expect(collectPendingImageSessionIds([])).toEqual([]);
  });
});

describe("expectedCandidatesForSession", () => {
  it("同一 image_session 取预期张数最大值，未知返回 null", () => {
    const events: AgentToolEvent[] = [
      toolEvent("generate_image", pendingResult("s1", 2)),
      toolEvent("generate_image", pendingResult("s1", 3)),
      toolEvent("generate_image", { pending: true, image_session_id: "s2" }),
      toolEvent("generate_image", pendingResult("s3", 4)),
    ];
    expect(expectedCandidatesForSession(events, "s1")).toBe(3);
    expect(expectedCandidatesForSession(events, "s2")).toBeNull();
    expect(expectedCandidatesForSession(events, "s3")).toBe(4);
    expect(expectedCandidatesForSession(events, "missing")).toBeNull();
  });
});

describe("candidatesFromImageSessionDetail", () => {
  it("取最新生成任务分组的已完成候选，按 candidate_index 排序", () => {
    const detail = detailWith(
      [
        { created_at: "2026-01-01T00:00:00Z", result_generation_group_id: "g-old" },
        { created_at: "2026-01-02T00:00:00Z", result_generation_group_id: "g-new" },
      ],
      [
        {
          generation_group_id: "g-new",
          candidate_index: 2,
          asset_id: "asset-2",
          created_at: "2026-01-02T00:05:00Z",
        },
        {
          generation_group_id: "g-old",
          candidate_index: 1,
          asset_id: "asset-old",
          created_at: "2026-01-01T00:05:00Z",
        },
        {
          generation_group_id: "g-new",
          candidate_index: 1,
          asset_id: "asset-1",
          created_at: "2026-01-02T00:03:00Z",
        },
      ],
    );
    expect(candidatesFromImageSessionDetail(detail, formatLabel)).toEqual([
      {
        asset_id: "asset-1",
        url: "/api/image-session-assets/asset-1/download",
        preview_url: "/api/image-session-assets/asset-1/download?variant=preview",
        label: "候选 1",
      },
      {
        asset_id: "asset-2",
        url: "/api/image-session-assets/asset-2/download",
        preview_url: "/api/image-session-assets/asset-2/download?variant=preview",
        label: "候选 2",
      },
    ]);
  });

  it("最新任务尚无分组号（首张候选未完成）或没有任务时不返回候选", () => {
    const running = detailWith(
      [{ created_at: "2026-01-02T00:00:00Z", result_generation_group_id: null }],
      [
        {
          generation_group_id: "g-old",
          candidate_index: 1,
          asset_id: "asset-old",
          created_at: "2026-01-01T00:05:00Z",
        },
      ],
    );
    expect(candidatesFromImageSessionDetail(running, formatLabel)).toEqual([]);
    expect(candidatesFromImageSessionDetail(detailWith([], []), formatLabel)).toEqual([]);
  });

  it("过滤尚无成图的轮次，空详情安全返回空数组", () => {
    const detail = detailWith(
      [{ created_at: "2026-01-01T00:00:00Z", result_generation_group_id: "g1" }],
      [
        {
          generation_group_id: "g1",
          candidate_index: 1,
          asset_id: null,
          created_at: "2026-01-01T00:01:00Z",
        },
        {
          generation_group_id: "g1",
          candidate_index: 2,
          asset_id: "asset-2",
          created_at: "2026-01-01T00:02:00Z",
        },
      ],
    );
    const candidates = candidatesFromImageSessionDetail(detail, formatLabel);
    expect(candidates).toHaveLength(1);
    expect(candidates[0].asset_id).toBe("asset-2");
    expect(candidatesFromImageSessionDetail(undefined as unknown as ImageSessionDetail, formatLabel)).toEqual([]);
  });
});

describe("mergeCandidatePollUpdate", () => {
  it("合并轮询更新并保留其他会话状态", () => {
    let state = mergeCandidatePollUpdate({}, "s1", { elapsedSeconds: 3 });
    expect(state.s1).toEqual({ candidates: [], elapsedSeconds: 3, expired: false });
    state = mergeCandidatePollUpdate(state, "s1", { elapsedSeconds: 6 });
    expect(state.s1.elapsedSeconds).toBe(6);
    const candidates = [{ asset_id: "a", url: "u", label: "候选 1" }];
    state = mergeCandidatePollUpdate(state, "s1", { candidates });
    expect(state.s1.candidates).toEqual(candidates);
    expect(state.s1.expired).toBe(false);
    state = mergeCandidatePollUpdate(state, "s2", { expired: true });
    expect(state.s1.expired).toBe(false);
    expect(state.s2).toEqual({ ...EMPTY_CANDIDATE_POLL_STATE, expired: true });
  });
});

describe("候选轮询器", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("轮询参数契约：3 秒间隔、最多 40 次（约 120 秒）", () => {
    expect(CANDIDATE_POLL_INTERVAL_MS).toBe(3000);
    expect(CANDIDATE_POLL_MAX_ATTEMPTS).toBe(40);
  });

  it("按间隔轮询，凑齐预期候选后回调并停止", async () => {
    const updates: CandidatePollUpdate[] = [];
    let calls = 0;
    const poller = createCandidatePoller({
      imageSessionId: "s1",
      fetchDetail: () => {
        calls += 1;
        return Promise.resolve(
          calls >= 3
            ? detailWith(
                [{ created_at: "2026-01-01T00:00:00Z", result_generation_group_id: "g1" }],
                [
                  {
                    generation_group_id: "g1",
                    candidate_index: 2,
                    asset_id: "asset-2",
                    created_at: "2026-01-01T00:02:00Z",
                  },
                  {
                    generation_group_id: "g1",
                    candidate_index: 1,
                    asset_id: "asset-1",
                    created_at: "2026-01-01T00:01:00Z",
                  },
                ],
              )
            : detailWith([], []),
        );
      },
      formatLabel,
      expectedCandidates: 2,
      onUpdate: (update) => updates.push(update),
    });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS);
    expect(calls).toBe(2);
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS);
    expect(calls).toBe(3);
    expect(updates).toEqual([
      { elapsedSeconds: 3 },
      { elapsedSeconds: 6 },
      {
        candidates: [
          {
            asset_id: "asset-1",
            url: "/api/image-session-assets/asset-1/download",
            preview_url: "/api/image-session-assets/asset-1/download?variant=preview",
            label: "候选 1",
          },
          {
            asset_id: "asset-2",
            url: "/api/image-session-assets/asset-2/download",
            preview_url: "/api/image-session-assets/asset-2/download?variant=preview",
            label: "候选 2",
          },
        ],
      },
    ]);
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 5);
    expect(calls).toBe(3); // 就绪后不再轮询
  });

  it("候选不足预期时继续等待，超限后交出部分候选", async () => {
    const updates: CandidatePollUpdate[] = [];
    let calls = 0;
    const poller = createCandidatePoller({
      imageSessionId: "s1",
      fetchDetail: () => {
        calls += 1;
        return Promise.resolve(
          detailWith(
            [{ created_at: "2026-01-01T00:00:00Z", result_generation_group_id: "g1" }],
            [
              {
                generation_group_id: "g1",
                candidate_index: 1,
                asset_id: "asset-1",
                created_at: "2026-01-01T00:01:00Z",
              },
            ],
          ),
        );
      },
      formatLabel,
      expectedCandidates: 2,
      maxAttempts: 3,
      onUpdate: (update) => updates.push(update),
    });
    poller.start();
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 2);
    expect(calls).toBe(3);
    expect(updates.at(-1)?.candidates).toHaveLength(1); // 部分候选也交给卡片展示
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 5);
    expect(calls).toBe(3); // 达到上限即停止，不无限轮询
  });

  it("超限且无候选时回调 expired 并停止轮询", async () => {
    const updates: CandidatePollUpdate[] = [];
    let calls = 0;
    const poller = createCandidatePoller({
      imageSessionId: "s1",
      fetchDetail: () => {
        calls += 1;
        return Promise.resolve(detailWith([], []));
      },
      formatLabel,
      maxAttempts: 2,
      onUpdate: (update) => updates.push(update),
    });
    poller.start();
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 3);
    expect(calls).toBe(2);
    expect(updates).toEqual([{ elapsedSeconds: 3 }, { expired: true }]);
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 5);
    expect(calls).toBe(2);
  });

  it("dispose 后不再轮询也不再回调（卸载清理）", async () => {
    const updates: CandidatePollUpdate[] = [];
    let calls = 0;
    const poller = createCandidatePoller({
      imageSessionId: "s1",
      fetchDetail: () => {
        calls += 1;
        return Promise.resolve(detailWith([], []));
      },
      formatLabel,
      onUpdate: (update) => updates.push(update),
    });
    poller.start();
    await vi.advanceTimersByTimeAsync(0);
    expect(calls).toBe(1);
    poller.dispose();
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS * 5);
    expect(calls).toBe(1);
    expect(updates).toEqual([]);
  });

  it("单次查询失败不中断轮询", async () => {
    const updates: CandidatePollUpdate[] = [];
    let calls = 0;
    const poller = createCandidatePoller({
      imageSessionId: "s1",
      fetchDetail: () => {
        calls += 1;
        if (calls === 1) return Promise.reject(new Error("network"));
        return Promise.resolve(
          detailWith(
            [{ created_at: "2026-01-01T00:00:00Z", result_generation_group_id: "g1" }],
            [
              {
                generation_group_id: "g1",
                candidate_index: 1,
                asset_id: "asset-1",
                created_at: "2026-01-01T00:01:00Z",
              },
            ],
          ),
        );
      },
      formatLabel,
      onUpdate: (update) => updates.push(update),
    });
    poller.start();
    await vi.advanceTimersByTimeAsync(CANDIDATE_POLL_INTERVAL_MS);
    expect(calls).toBe(2);
    expect(updates.at(-1)?.candidates).toHaveLength(1);
  });
});
