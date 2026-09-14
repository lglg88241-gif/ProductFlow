import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { AgentToolEvent } from "../lib/agentTypes";
import { translate } from "../lib/i18n";
import { PosterRerenderCard, isPosterRerenderError, posterRerenderChangedFields } from "./PosterRerenderCard";

function toolEvent(result: AgentToolEvent["result"]): AgentToolEvent {
  return { tool: "rerender_poster_copy", result };
}

describe("PosterRerenderCard 失败形态", () => {
  it("status=error 时按错误展示惯例显示 message", () => {
    const result = { status: "error", message: "海报不存在或已被删除" };
    expect(isPosterRerenderError(result)).toBe(true);
    const html = renderToStaticMarkup(createElement(PosterRerenderCard, { event: toolEvent(result) }));
    expect(html).toContain("海报不存在或已被删除");
    expect(html).toContain("bg-amber-50");
  });

  it("message 缺省时显示兜底文案", () => {
    const html = renderToStaticMarkup(createElement(PosterRerenderCard, { event: toolEvent({ status: "error" }) }));
    expect(html).toContain(translate("zh-CN", "workbench.rerender.failed"));
  });
});

describe("PosterRerenderCard 成功形态", () => {
  it("展示下载链接与变更字段", () => {
    const result = {
      status: "ok",
      poster_id: "12",
      poster_kind: "moments",
      download_url: "/api/posters/12/download",
      changed_fields: ["title", "hashtags"],
    };
    expect(isPosterRerenderError(result)).toBe(false);
    expect(posterRerenderChangedFields(result)).toEqual(["title", "hashtags"]);
    const html = renderToStaticMarkup(createElement(PosterRerenderCard, { event: toolEvent(result) }));
    expect(html).toContain('href="/api/posters/12/download"');
    expect(html).toContain(translate("zh-CN", "workbench.rerender.updated"));
    expect(html).toContain("变更字段: title、hashtags");
  });

  it("changed_fields 缺省或含脏数据时安全降级", () => {
    expect(posterRerenderChangedFields({})).toEqual([]);
    const dirty = { changed_fields: ["title", 42, null] } as unknown as AgentToolEvent["result"];
    expect(posterRerenderChangedFields(dirty)).toEqual(["title"]);
  });

  it("非失败且无下载地址时不渲染", () => {
    const html = renderToStaticMarkup(createElement(PosterRerenderCard, { event: toolEvent({ status: "ok" }) }));
    expect(html).toBe("");
  });
});
