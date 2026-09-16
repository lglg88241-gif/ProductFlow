import type { AgentMessage, AgentToolEvent } from "./agentTypes";

/**
 * 会话历史 → 可渲染时间线（审计 E：刷新后工具卡片不丢）。
 *
 * 背景：工具结果本来就落库为 role="tool" 的消息（含 tool_name 与结果 JSON），
 * 但工作台此前把这类消息整体过滤掉，于是刷新页面后候选对比卡、文案报告卡、
 * 分格导出卡全部消失——用户以为产出丢了。这里把持久化的工具消息还原成卡片事件，
 * 与普通消息按原始顺序合并成一条时间线。
 */

export type TimelineItem =
  | { kind: "message"; key: string; message: AgentMessage }
  | { kind: "tool"; key: string; event: AgentToolEvent };

/** 解析落库的工具结果 JSON；解析不出来（历史脏数据）返回 null 并跳过该条。 */
function parseToolResult(content: string): AgentToolEvent["result"] | null {
  try {
    const parsed = JSON.parse(content) as unknown;
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as AgentToolEvent["result"];
    }
    return null;
  } catch {
    return null;
  }
}

export function buildTimeline(messages: AgentMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  for (const message of messages) {
    if (message.role === "tool") {
      const result = parseToolResult(message.content);
      if (result === null || !message.tool_name) continue;
      items.push({ kind: "tool", key: message.id, event: { tool: message.tool_name, result } });
      continue;
    }
    // 带工具调用的 assistant 消息内容为空（工具结果在随后的 tool 消息里），
    // 不渲染成空气泡，避免历史里出现一串空框
    if (message.role === "assistant" && message.content.trim() === "") continue;
    items.push({ kind: "message", key: message.id, message });
  }
  return items;
}

/** 历史里是否存在可展示的产出（用于空态判定：只有工具卡也算"有内容"）。 */
export function hasVisibleContent(items: TimelineItem[]): boolean {
  return items.length > 0;
}
