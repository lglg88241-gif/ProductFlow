import { describe, expect, it } from "vitest";

import type { AgentMessage } from "./agentTypes";
import { buildTimeline, hasVisibleContent } from "./agentTimeline";

function msg(partial: Partial<AgentMessage> & Pick<AgentMessage, "id" | "role" | "content">): AgentMessage {
  return {
    tool_name: null,
    image_session_id: null,
    created_at: "2026-09-16T00:00:00Z",
    ...partial,
  };
}

describe("会话历史时间线重建（刷新后卡片不丢）", () => {
  it("持久化的工具结果被还原成卡片事件", () => {
    const timeline = buildTimeline([
      msg({ id: "u1", role: "user", content: "做张海报" }),
      msg({ id: "a1", role: "assistant", content: "" }),
      msg({
        id: "t1",
        role: "tool",
        content: JSON.stringify({ status: "ok", report_id: "r1", title: "报告" }),
        tool_name: "write_copy_report",
      }),
      msg({ id: "a2", role: "assistant", content: "报告写好了。" }),
    ]);

    expect(timeline.map((item) => item.kind)).toEqual(["message", "tool", "message"]);
    const toolItem = timeline[1];
    expect(toolItem.kind === "tool" && toolItem.event.tool).toBe("write_copy_report");
    expect(toolItem.kind === "tool" && toolItem.event.result.report_id).toBe("r1");
  });

  it("内容为空的 assistant（仅工具调用）不渲染成空气泡", () => {
    const timeline = buildTimeline([
      msg({ id: "a1", role: "assistant", content: "" }),
      msg({ id: "a2", role: "assistant", content: "   " }),
      msg({ id: "a3", role: "assistant", content: "有话说" }),
    ]);
    expect(timeline).toHaveLength(1);
    expect(timeline[0].kind === "message" && timeline[0].message.content).toBe("有话说");
  });

  it("脏数据（结果不是合法 JSON 或缺工具名）被跳过而不炸", () => {
    const timeline = buildTimeline([
      msg({ id: "t1", role: "tool", content: "not-json", tool_name: "write_copy" }),
      msg({ id: "t2", role: "tool", content: '{"a":1}', tool_name: null }),
      msg({ id: "t3", role: "tool", content: "[1,2,3]", tool_name: "write_copy" }),
      msg({ id: "u1", role: "user", content: "你好" }),
    ]);
    expect(timeline).toHaveLength(1);
    expect(timeline[0].kind).toBe("message");
  });

  it("顺序保持原始消息顺序（卡片出现在它实际发生的位置）", () => {
    const timeline = buildTimeline([
      msg({ id: "u1", role: "user", content: "一" }),
      msg({ id: "t1", role: "tool", content: '{"status":"ok"}', tool_name: "generate_image" }),
      msg({ id: "a1", role: "assistant", content: "出图了" }),
      msg({ id: "t2", role: "tool", content: '{"status":"ok"}', tool_name: "export_moments_grid" }),
    ]);
    expect(timeline.map((item) => item.key)).toEqual(["u1", "t1", "a1", "t2"]);
  });

  it("空态判定：只有工具卡也算有内容", () => {
    const onlyTool = buildTimeline([
      msg({ id: "t1", role: "tool", content: '{"status":"ok"}', tool_name: "generate_image" }),
    ]);
    expect(hasVisibleContent(onlyTool)).toBe(true);
    expect(hasVisibleContent(buildTimeline([]))).toBe(false);
  });
});
