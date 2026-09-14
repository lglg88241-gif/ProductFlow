import type { AgentToolEvent } from "../lib/agentTypes";
import { useI18n } from "../lib/preferences";

/** tool_result 是否携带文案报告下载信息（契约新增 report_id，存在即渲染本卡）。 */
export function hasCopyReportResult(result: AgentToolEvent["result"]): boolean {
  return result.report_id != null;
}

/** 文案报告下载地址（缺省或非字符串时返回 null）。 */
export function copyReportDownloadUrl(result: AgentToolEvent["result"]): string | null {
  const url = result.download_url;
  return typeof url === "string" && url.length > 0 ? url : null;
}

/** 文案报告卡：write_copy_report 的标题、预览正文与下载入口。 */
export function CopyReportCard({ event }: { event: AgentToolEvent }) {
  const { t } = useI18n();
  if (!hasCopyReportResult(event.result)) return null;
  const result = event.result;
  const downloadUrl = copyReportDownloadUrl(result);
  const preview = typeof result.preview === "string" ? result.preview : "";
  return (
    <div className="mt-2 space-y-2 rounded-xl border border-slate-200 bg-white p-3 text-sm">
      <div className="flex items-center justify-between gap-3">
        <p className="min-w-0 truncate text-base font-semibold text-slate-900">
          {typeof result.title === "string" && result.title.length > 0
            ? result.title
            : t("workbench.copyReport.title")}
        </p>
        {downloadUrl ? (
          <a
            href={downloadUrl}
            download
            className="shrink-0 rounded-lg bg-slate-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-slate-700"
          >
            {t("workbench.download")}
          </a>
        ) : null}
      </div>
      {preview.length > 0 ? <p className="whitespace-pre-wrap text-slate-800">{preview}</p> : null}
    </div>
  );
}
