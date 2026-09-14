import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { AgentToolEvent } from "../lib/agentTypes";
import { CopyReportCard, copyReportDownloadUrl, hasCopyReportResult } from "./CopyReportCard";

function toolEvent(result: AgentToolEvent["result"]): AgentToolEvent {
  return { tool: "write_copy_report", result };
}

const reportResult = {
  report_id: 3,
  title: "国庆活动文案报告",
  download_url: "/api/agent/copy-reports/3/download",
  preview: "主标题：国庆大促\n副标题：全场八折",
};

describe("CopyReportCard 渲染条件", () => {
  it("tool_result 含 report_id 时判定为报告结果", () => {
    expect(hasCopyReportResult(reportResult)).toBe(true);
    expect(hasCopyReportResult({})).toBe(false);
    expect(hasCopyReportResult({ report: { headline: "旧字段" } })).toBe(false);
  });

  it("无 report_id 时不渲染", () => {
    const html = renderToStaticMarkup(
      createElement(CopyReportCard, { event: toolEvent({ report: { headline: "旧字段" } }) }),
    );
    expect(html).toBe("");
  });
});

describe("CopyReportCard 渲染内容", () => {
  it("下载链接指向 download_url 且带 download 属性", () => {
    expect(copyReportDownloadUrl(reportResult)).toBe("/api/agent/copy-reports/3/download");
    expect(copyReportDownloadUrl({})).toBeNull();
    const html = renderToStaticMarkup(createElement(CopyReportCard, { event: toolEvent(reportResult) }));
    expect(html).toContain('href="/api/agent/copy-reports/3/download"');
    expect(html).toContain('download=""');
  });

  it("渲染标题与 preview 正文（pre-wrap 折行展示）", () => {
    const html = renderToStaticMarkup(createElement(CopyReportCard, { event: toolEvent(reportResult) }));
    expect(html).toContain("国庆活动文案报告");
    expect(html).toContain("主标题：国庆大促");
    expect(html).toContain("副标题：全场八折");
  });

  it("title 缺省时回退到默认标题", () => {
    const html = renderToStaticMarkup(
      createElement(CopyReportCard, {
        event: toolEvent({ report_id: 5, download_url: "/api/agent/copy-reports/5/download" }),
      }),
    );
    expect(html).toContain("文案报告");
  });
});
