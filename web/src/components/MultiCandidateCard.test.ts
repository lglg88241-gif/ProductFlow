import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { AgentToolEvent } from "../lib/agentTypes";
import { translate } from "../lib/i18n";
import {
  MultiCandidateCard,
  candidatesFromResult,
  pendingCandidateCount,
  shouldRenderMultiCandidates,
} from "./MultiCandidateCard";

function toolEvent(result: AgentToolEvent["result"]): AgentToolEvent {
  return { tool: "generate_image", result };
}

function candidate(assetId: number) {
  return { asset_id: assetId, url: `https://cdn.example.com/${assetId}.png`, label: `方案 ${assetId}` };
}

describe("MultiCandidateCard 渲染条件", () => {
  it("候选多于 1 张时出卡，恰好 1 张保持原渲染不套新卡", () => {
    expect(shouldRenderMultiCandidates({ candidates: [candidate(1), candidate(2)] })).toBe(true);
    expect(shouldRenderMultiCandidates({ candidates: [candidate(1)] })).toBe(false);
    expect(shouldRenderMultiCandidates({})).toBe(false);
  });

  it("候选为空且 pending 时渲染生成中占位", () => {
    const pending = { candidates: [], pending: true, expected_candidates: 3 };
    expect(shouldRenderMultiCandidates(pending)).toBe(true);
    expect(pendingCandidateCount(pending)).toBe(3);
    expect(pendingCandidateCount({ pending: true })).toBeNull();
    const html = renderToStaticMarkup(
      createElement(MultiCandidateCard, { event: toolEvent(pending), onContinue: () => undefined }),
    );
    expect(html).toContain(translate("zh-CN", "workbench.candidate.pending", { count: 3 }));
  });
});

describe("MultiCandidateCard 渲染内容", () => {
  it("渲染图片预览、label、下载与用这张继续按钮", () => {
    const html = renderToStaticMarkup(
      createElement(MultiCandidateCard, {
        event: toolEvent({ candidates: [candidate(1), candidate(2)] }),
        onContinue: () => undefined,
      }),
    );
    expect(html).toContain('src="https://cdn.example.com/1.png"');
    expect(html).toContain("方案 1");
    expect(html).toContain("方案 2");
    expect(html).toContain('href="https://cdn.example.com/1.png"');
    expect(html).toContain("下载");
    expect(html).toContain(translate("zh-CN", "workbench.candidate.useThis"));
  });

  it("“用这张继续”的预填文案按契约拼接候选 url", () => {
    expect(translate("zh-CN", "workbench.candidate.continueWith", { url: candidate(1).url })).toBe(
      "我想基于这张继续修改：https://cdn.example.com/1.png",
    );
  });

  it("candidatesFromResult 过滤缺 url 的脏数据", () => {
    const dirty = { candidates: [candidate(1), { asset_id: 9, label: "坏数据" }, null] } as unknown as AgentToolEvent["result"];
    expect(candidatesFromResult(dirty)).toEqual([candidate(1)]);
    expect(candidatesFromResult({})).toEqual([]);
  });
});
