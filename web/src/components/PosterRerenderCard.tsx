import type { AgentToolEvent } from "../lib/agentTypes";
import { useI18n } from "../lib/preferences";

/** rerender_poster_copy 是否为失败形态（契约：{"status":"error","message"}）。 */
export function isPosterRerenderError(result: AgentToolEvent["result"]): boolean {
  return result.status === "error";
}

/** 成功形态里实际变更的字段列表（过滤非字符串脏数据）。 */
export function posterRerenderChangedFields(result: AgentToolEvent["result"]): string[] {
  const fields = result.changed_fields;
  if (!Array.isArray(fields)) return [];
  return fields.filter((field): field is string => typeof field === "string");
}

/** 重绘海报文案结果卡：失败按错误惯例展示 message，成功展示下载链接与变更字段。 */
export function PosterRerenderCard({ event }: { event: AgentToolEvent }) {
  const { t } = useI18n();
  const result = event.result;
  if (isPosterRerenderError(result)) {
    return (
      <div className="mt-2 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-700">
        {typeof result.message === "string" && result.message.length > 0
          ? result.message
          : t("workbench.rerender.failed")}
      </div>
    );
  }
  const url = result.download_url;
  if (typeof url !== "string" || url.length === 0) return null;
  const changedFields = posterRerenderChangedFields(result);
  return (
    <div className="mt-2 flex items-center justify-between gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-3 py-2">
      <div className="min-w-0 text-sm text-emerald-800">
        <p className="font-medium">{t("workbench.rerender.updated")}</p>
        {changedFields.length > 0 ? (
          <p className="text-xs text-emerald-600">
            {t("workbench.rerender.changedFields")}: {changedFields.join("、")}
          </p>
        ) : null}
      </div>
      <a
        href={url}
        className="shrink-0 rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-700"
      >
        {t("workbench.download")}
      </a>
    </div>
  );
}
