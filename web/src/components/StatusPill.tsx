import type { ProductWorkflowState } from "../lib/types";
import { useI18n } from "../lib/preferences";

const CONFIG: Record<
  ProductWorkflowState,
  { textKey: "status.draft" | "status.copyReady" | "status.posterReady" | "status.failed"; classes: string; dot: string }
> = {
  draft: {
    textKey: "status.draft",
    classes: "bg-zinc-100 text-zinc-600 border-zinc-200",
    dot: "bg-zinc-400",
  },
  copy_ready: {
    textKey: "status.copyReady",
    classes: "bg-blue-50 text-blue-700 border-blue-200",
    dot: "bg-blue-500",
  },
  poster_ready: {
    textKey: "status.posterReady",
    classes: "bg-emerald-50 text-emerald-700 border-emerald-200",
    dot: "bg-emerald-500",
  },
  failed: {
    textKey: "status.failed",
    classes: "bg-red-50 text-red-700 border-red-200",
    dot: "bg-red-500",
  },
};

export function StatusPill({ status }: { status: ProductWorkflowState }) {
  const { t } = useI18n();
  const config = CONFIG[status];
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] font-medium ${config.classes}`}>
      <span className={`mr-1.5 h-1.5 w-1.5 rounded-full ${config.dot}`} />
      {t(config.textKey)}
    </span>
  );
}
