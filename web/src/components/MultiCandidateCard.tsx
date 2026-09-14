import { Loader2 } from "lucide-react";

import type { AgentToolEvent } from "../lib/agentTypes";
import type { CandidatePollState } from "../lib/candidatePolling";
import { useI18n } from "../lib/preferences";

/** generate_image tool_result 中的单张候选图（契约：asset_id / url / label，主键为 UUID 字符串）。 */
export interface AgentCandidateImage {
  asset_id: string;
  url: string;
  label: string;
  /** 轮询补全的候选会带预览地址，用于 <img> 加载；缺省时回退到 url。 */
  preview_url?: string;
}

/** 从 tool_result 中安全取出候选数组，过滤缺 url 的脏数据（可能为空数组或缺省）。 */
export function candidatesFromResult(result: AgentToolEvent["result"]): AgentCandidateImage[] {
  const raw = result.candidates;
  if (!Array.isArray(raw)) return [];
  return raw.filter(
    (item): item is AgentCandidateImage =>
      typeof item === "object" && item !== null && typeof (item as AgentCandidateImage).url === "string",
  );
}

/** 是否渲染候选对比卡：候选多于 1 张；或异步生成中且暂无候选（恰好 1 张保持原渲染）。 */
export function shouldRenderMultiCandidates(result: AgentToolEvent["result"]): boolean {
  const candidates = candidatesFromResult(result);
  if (candidates.length > 1) return true;
  return candidates.length === 0 && result.pending === true;
}

/** pending 态的预期候选张数（expected_candidates 缺省时返回 null）。 */
export function pendingCandidateCount(result: AgentToolEvent["result"]): number | null {
  const expected = result.expected_candidates;
  return typeof expected === "number" && Number.isFinite(expected) ? expected : null;
}

/** pending 态占位：轮询中显示已等待秒数，超限或无轮询能力时显示对应兜底文案。 */
function PendingPlaceholder({ text, expired = false }: { text: string; expired?: boolean }) {
  if (expired) {
    return (
      <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
        {text}
      </div>
    );
  }
  return (
    <div className="mt-2 flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-500">
      <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
      {text}
    </div>
  );
}

/** 候选对比卡：多候选并排预览对比，或异步生成中的占位提示（轮询就绪后自动换成候选卡）。 */
export function MultiCandidateCard({
  event,
  onContinue,
  poll,
}: {
  event: AgentToolEvent;
  onContinue: (message: string) => void;
  /** 容器按 image_session_id 去重轮询得到的进度；缺省时退回纯静态 pending 提示。 */
  poll?: CandidatePollState;
}) {
  const { t } = useI18n();
  const resultCandidates = candidatesFromResult(event.result);
  const candidates = resultCandidates.length > 0 ? resultCandidates : (poll?.candidates ?? []);
  if (candidates.length === 0) {
    if (event.result.pending !== true) return null;
    const count = pendingCandidateCount(event.result) ?? 0;
    if (poll?.expired) {
      return <PendingPlaceholder expired text={t("workbench.candidate.pendingTimeout")} />;
    }
    if (poll) {
      return (
        <PendingPlaceholder
          text={t("workbench.candidate.pendingWaiting", { count, seconds: poll.elapsedSeconds })}
        />
      );
    }
    return <PendingPlaceholder text={t("workbench.candidate.pending", { count })} />;
  }
  return (
    <div className="mt-2 space-y-2 rounded-xl border border-slate-200 bg-white p-3">
      <p className="text-xs font-medium text-slate-500">{t("workbench.candidates")}</p>
      <div className="grid gap-2 sm:grid-cols-3">
        {candidates.map((candidate, index) => (
          <div
            key={candidate.asset_id ?? index}
            className="overflow-hidden rounded-xl border border-slate-200 bg-white"
          >
            <a href={candidate.url} target="_blank" rel="noreferrer" title={candidate.label}>
              <img
                src={candidate.preview_url ?? candidate.url}
                alt={candidate.label}
                className="h-28 w-full object-cover"
              />
            </a>
            <div className="space-y-1 p-2">
              <p className="line-clamp-1 text-xs font-medium text-slate-800">{candidate.label}</p>
              <div className="flex gap-1">
                <a
                  href={candidate.url}
                  className="flex-1 rounded-lg border border-slate-200 px-2 py-1 text-center text-xs font-medium text-slate-700 hover:border-slate-400"
                >
                  {t("workbench.download")}
                </a>
                <button
                  type="button"
                  onClick={() => onContinue(t("workbench.candidate.continueWith", { url: candidate.url }))}
                  className="flex-1 rounded-lg bg-slate-900 px-2 py-1 text-xs font-medium text-white hover:bg-slate-700"
                >
                  {t("workbench.candidate.useThis")}
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
