"""SSE 心跳保活测试：LLM/工具执行期间流空闲时输出 ": ping" 注释帧。

nginx 默认 60s 空闲断开连接；agent 一轮实测 45~157 秒且工具执行期间无帧。
`_heartbeat_frames` 用后台线程消费事件生成器、响应生成器空闲时发注释帧保活；
对应前端解析跳过注释行的用例见 web/src/lib/api.test.ts。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator

from fastapi.testclient import TestClient
from helpers import _login

from productflow_backend.application.designer_agent.llm import AgentLLMResponse


def test_heartbeat_frames_emitted_when_source_blocks() -> None:
    """事件源阻塞超过心跳间隔时应输出 ": ping" 注释帧，且真实帧按顺序透传。"""
    from productflow_backend.presentation.routes.agent import _heartbeat_frames

    def _blocking_source() -> Generator[str, None, None]:
        yield "event: message\ndata: {}\n\n"
        time.sleep(0.12)  # 模拟 LLM/工具执行期间无帧输出
        yield "event: done\ndata: {}\n\n"

    frames = list(_heartbeat_frames(_blocking_source(), heartbeat_interval=0.02))

    assert ": ping" in "".join(frames)
    # 心跳帧必须是注释帧，不能伪装成事件帧
    assert all(frame.startswith(":") or frame.startswith("event:") for frame in frames)
    real_frames = [frame for frame in frames if frame.startswith("event:")]
    assert real_frames[0] == "event: message\ndata: {}\n\n"
    assert real_frames[-1] == "event: done\ndata: {}\n\n"


def test_heartbeat_generator_closes_and_joins_pump_thread() -> None:
    """客户端断开（GeneratorExit）时置停止标志并 join 后台线程，等待 turn 消费完。"""
    from productflow_backend.presentation.routes.agent import _heartbeat_frames

    source_finished = threading.Event()

    def _slow_source() -> Generator[str, None, None]:
        try:
            for index in range(5):
                time.sleep(0.02)
                yield f"event: tick\ndata: {{\"i\": {index}}}\n\n"
        finally:
            source_finished.set()

    gen = _heartbeat_frames(_slow_source(), heartbeat_interval=0.02)
    # 泵线程启动可能慢于心跳间隔，此时首个产出是心跳注释帧（正常行为）：
    # 读到真实事件帧为止，避免对"第一帧"的时序断言造成负载相关的偶发失败。
    first_real_frame = None
    for _ in range(50):
        frame = next(gen)
        if frame.startswith("event:"):
            first_real_frame = frame
            break
    assert first_real_frame is not None and first_real_frame.startswith("event: tick")

    gen.close()  # 模拟客户端断开触发 GeneratorExit

    assert source_finished.is_set(), "close 后后台线程应把事件源执行完（turn 继续落库的语义）"


def test_agent_message_stream_emits_heartbeat_comments(
    configured_env,
    install_scripted_llm,
    monkeypatch,
) -> None:
    """端到端：LLM 执行期间无帧时流里出现 ": ping" 注释帧，且事件解析不受影响。"""
    import productflow_backend.presentation.routes.agent as agent_route
    from productflow_backend.presentation.api import create_app

    monkeypatch.setattr(agent_route, "DEFAULT_SSE_HEARTBEAT_INTERVAL_SECONDS", 0.02)

    llm = install_scripted_llm(
        [
            AgentLLMResponse(content="我先确认一下需求哦。"),  # 纯文字 → 触发督促重试
            AgentLLMResponse(content="好的，请告诉我具体要做哪种海报。"),
        ]
    )
    original_chat = llm.chat

    def _slow_chat(**kwargs):
        time.sleep(0.2)  # 模拟阻塞执行：期间事件流无帧
        return original_chat(**kwargs)

    monkeypatch.setattr(llm, "chat", _slow_chat)

    app = create_app()
    client = TestClient(app)
    _login(client)
    session_id = client.post("/api/agent/sessions", json={"title": "心跳"}).json()["id"]

    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/messages/stream",
        json={"content": "做个开业海报"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert ": ping" in body
    assert "event: done" in body

    # 注释帧不应破坏事件解析：跳过纯注释块后仍能取到完整事件序列
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        event_lines = [line for line in lines if line.startswith("event: ")]
        if not event_lines:
            continue  # 纯注释块（心跳）
        data_lines = [line for line in lines if line.startswith("data: ")]
        events.append((event_lines[0][len("event: ") :], json.loads(data_lines[0][len("data: ") :])))
    kinds = [event for event, _ in events]
    assert kinds[0] == "message"  # user 落库帧
    assert kinds[-1] == "done"
    assert any(event == "message" and data["role"] == "assistant" for event, data in events)
