import { afterEach, describe, expect, it, vi } from "vitest";

import { api } from "./api";

/** 构造一个 SSE 响应：按给定分片顺序写入 text/event-stream。 */
function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
}

describe("streamAgentMessage SSE 解析", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("跳过心跳注释帧，不派发任何事件", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => sseResponse([": ping\n\n: ping\n\n"])));

    const events: Array<[string, Record<string, unknown>]> = [];
    const done = await api.streamAgentMessage("s1", "你好", (event, data) => events.push([event, data]));

    expect(events).toEqual([]);
    expect(done).toBeNull();
  });

  it("混合流：注释帧被跳过，真实事件与 done 帧正常解析", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        sseResponse([
          ': ping\n\nevent: message\ndata: {"id":"m1","role":"user","content":"你好"}\n\n: ping\n\n',
          'event: done\ndata: {"session_id":"s1","stage":"clarify"}\n\n',
        ]),
      ),
    );

    const events: Array<[string, Record<string, unknown>]> = [];
    const done = await api.streamAgentMessage("s1", "你好", (event, data) => events.push([event, data]));

    expect(events.map(([event]) => event)).toEqual(["message", "done"]);
    expect(events[0][1]).toMatchObject({ role: "user", content: "你好" });
    expect(done).toEqual({ session_id: "s1", stage: "clarify" });
  });

  it("注释帧跨 chunk 分割时同样安全", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => sseResponse([": pi", "ng\n\nevent: done\ndata: {}\n\n"])),
    );

    const events: Array<[string, Record<string, unknown>]> = [];
    const done = await api.streamAgentMessage("s1", "你好", (event, data) => events.push([event, data]));

    expect(events).toEqual([["done", {}]]);
    expect(done).toEqual({});
  });
});
