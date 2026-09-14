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

function candidate(assetId: string) {
  return { asset_id: assetId, url: `https://cdn.example.com/${assetId}.png`, label: `方案 ${assetId}` };
}

describe("MultiCandidateCard 渲染条件", () => {
  it("候选多于 1 张时出卡，恰好 1 张保持原渲染不套新卡", () => {
    expect(shouldRenderMultiCandidates({ candidates: [candidate("1"), candidate("2")] })).toBe(true);
    expect(shouldRenderMultiCandidates({ candidates: [candidate("1")] })).toBe(false);
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
        event: toolEvent({ candidates: [candidate("1"), candidate("2")] }),
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
    expect(translate("zh-CN", "workbench.candidate.continueWith", { url: candidate("1").url })).toBe(
      "我想基于这张继续修改：https://cdn.example.com/1.png",
    );
  });

  it("candidatesFromResult 过滤缺 url 的脏数据", () => {
    const dirty = { candidates: [candidate("1"), { asset_id: "9", label: "坏数据" }, null] } as unknown as AgentToolEvent["result"];
    expect(candidatesFromResult(dirty)).toEqual([candidate("1")]);
    expect(candidatesFromResult({})).toEqual([]);
  });
});

describe("MultiCandidateCard 轮询态", () => {
  const polledCandidates = [
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
  ];

  function renderWithPoll(result: AgentToolEvent["result"], poll?: { candidates: typeof polledCandidates; elapsedSeconds: number; expired: boolean }) {
    return renderToStaticMarkup(
      createElement(MultiCandidateCard, {
        event: toolEvent(result),
        onContinue: () => undefined,
        poll,
      }),
    );
  }

  it("pending 轮询中显示等待秒数的动态文案", () => {
    const html = renderWithPoll(
      { candidates: [], pending: true, expected_candidates: 2, image_session_id: "s1" },
      { candidates: [], elapsedSeconds: 9, expired: false },
    );
    expect(html).toContain(translate("zh-CN", "workbench.candidate.pendingWaiting", { count: 2, seconds: 9 }));
    expect(html).not.toContain(translate("zh-CN", "workbench.candidate.pending", { count: 2 }));
  });

  it("轮询超限后显示兜底文案并停止等待提示", () => {
    const html = renderWithPoll(
      { candidates: [], pending: true, expected_candidates: 3, image_session_id: "s1" },
      { candidates: [], elapsedSeconds: 120, expired: true },
    );
    expect(html).toContain(translate("zh-CN", "workbench.candidate.pendingTimeout"));
    expect(html).not.toContain(translate("zh-CN", "workbench.candidate.pendingWaiting", { count: 3, seconds: 120 }));
  });

  it("轮询到候选后渲染候选卡：预览图、下载与用这张继续", () => {
    const html = renderWithPoll(
      { candidates: [], pending: true, expected_candidates: 2, image_session_id: "s1" },
      { candidates: polledCandidates, elapsedSeconds: 12, expired: false },
    );
    expect(html).toContain('src="/api/image-session-assets/asset-1/download?variant=preview"');
    expect(html).toContain('href="/api/image-session-assets/asset-1/download"');
    expect(html).toContain("候选 1");
    expect(html).toContain("候选 2");
    expect(html).toContain("下载");
    expect(html).toContain(translate("zh-CN", "workbench.candidate.useThis"));
  });

  it("result 自带候选时优先于轮询候选", () => {
    const html = renderWithPoll(
      { candidates: [candidate("9")], pending: true, image_session_id: "s1" },
      { candidates: polledCandidates, elapsedSeconds: 12, expired: false },
    );
    expect(html).toContain('src="https://cdn.example.com/9.png"');
    expect(html).not.toContain('src="/api/image-session-assets/asset-1/download?variant=preview"');
  });
});
