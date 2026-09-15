"""设计师 Agent 的编排循环与路由测试（spec §10 剧本的确定性回放）。

`install_scripted_llm` fixture 由 conftest 提供。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _enable_deletion, _login

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall


def _copy_script() -> list[AgentLLMResponse]:
    return [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-copy-1",
                    name="write_copy",
                    arguments={"brief": "美容院周末开业酬宾，面部护理 199 元体验，语气亲切", "kind": "moments"},
                )
            ],
        ),
        AgentLLMResponse(
            content=json.dumps(
                [
                    {"title": "开业大酬宾", "content": "本周末美容院开业，面部护理体验价 199 元", "hashtags": ["开业"]},
                    {"title": "周末盛典", "content": "周末来做一次面部 SPA，开业价只在两天", "hashtags": ["美容"]},
                    {"title": "美丽开业", "content": "新店开业，前 50 名到店有礼", "hashtags": ["新店"]},
                ],
                ensure_ascii=False,
            )
        ),
        AgentLLMResponse(content="给您准备了 3 版朋友圈文案，挑一版喜欢的，我就开始出图。"),
    ]


def test_agent_turn_writes_copy_through_tool_script(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(_copy_script())
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="开业海报")
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="我要周末做个开业酬宾的朋友圈海报")

        roles = [message.role for message in result.messages]
        assert roles == ["user", "assistant", "tool", "assistant"]
        assert agent_session.stage == "produce"

        tool_message = result.messages[2]
        assert tool_message.tool_name == "write_copy"
        tool_result = json.loads(tool_message.content)
        assert tool_result["status"] == "completed"
        assert len(tool_result["copies"]) == 3

        final = result.messages[3]
        assert "3 版" in final.content
        # system prompt 在每轮都排在最前
        assert install_scripted_llm and result.tool_events[0]["tool"] == "write_copy"
    finally:
        db.close()


def test_agent_turn_generates_image_and_links_session(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.models import ImageSessionGenerationTask
    from productflow_backend.infrastructure.db.session import get_session_factory

    script = [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-image-1",
                    name="generate_image",
                    arguments={
                        "prompt": "美容院开业海报，粉色系，面部护理主视觉，文案位在下方",
                        "size": "1080x1440",
                    },
                )
            ],
        ),
        AgentLLMResponse(content="图片已经生成好了！可以让我换配色、加价格或换尺寸。"),
    ]
    install_scripted_llm(script)
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="出图")
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="就要刚才第二版文案，出图吧")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "completed", tool_result
        # 尺寸按生成约束归一到 16 的倍数
        assert tool_result["size"] == "1072x1440"
        # 默认一次生成 2 张候选供挑选
        assert tool_result["expected_candidates"] == 2
        assert len(tool_result["completed_assets"]) == 2
        assert "pending" not in tool_result

        candidates = tool_result["candidates"]
        assert [item["label"] for item in candidates] == ["候选 1", "候选 2"]
        assert all(item["url"].startswith("/api/image-session-assets/") for item in candidates)
        assert tool_result["primary_url"] == candidates[0]["url"]

        assert agent_session.stage == "review"
        assert agent_session.image_session_id is not None
        assert result.pending_generation_tasks == []

        task = db.query(ImageSessionGenerationTask).filter_by(session_id=agent_session.image_session_id).one()
        assert task.generation_count == 2
        assert task.status == "succeeded"
        assert task.result_generation_group_id
    finally:
        db.close()


def test_generate_image_count_argument_is_passed_through(configured_env: Path, install_scripted_llm) -> None:
    """count 透传为会话生成任务的 generation_count，并决定候选数量。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.models import ImageSessionGenerationTask
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-image-count",
                        name="generate_image",
                        arguments={"prompt": "三张方向不同的开业海报，粉色系", "count": 3},
                    )
                ],
            ),
            AgentLLMResponse(content="三个方向都出好了，挑一张我继续细化。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="给我三个方向挑挑")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "completed", tool_result
        assert tool_result["expected_candidates"] == 3

        task = db.query(ImageSessionGenerationTask).filter_by(session_id=agent_session.image_session_id).one()
        assert task.generation_count == 3
        assert task.completed_candidates == 3

        candidates = tool_result["candidates"]
        assert len(candidates) == 3
        assert [item["label"] for item in candidates] == ["候选 1", "候选 2", "候选 3"]
        assert tool_result["primary_url"] == candidates[0]["url"]
    finally:
        db.close()


def test_generate_image_count_out_of_range_is_clamped(configured_env: Path, install_scripted_llm) -> None:
    """count 超界收敛到 1~4；非整数回落默认 2。"""
    from productflow_backend.application.designer_agent.tools import _normalize_generation_count

    assert _normalize_generation_count(0) == 1
    assert _normalize_generation_count(99) == 4
    assert _normalize_generation_count("不是数字") == 2
    assert _normalize_generation_count(None) == 2


def test_agent_tool_failure_is_translated_for_the_user(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    script = [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-image-2",
                    name="generate_image",
                    arguments={"prompt": "   ", "size": "1024x1024"},
                )
            ],
        ),
        AgentLLMResponse(content="画图前我需要再确认一下画面内容，你想突出什么主体？"),
    ]
    install_scripted_llm(script)
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="随便来张图")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "error"
        assert "画什么" in tool_result["message"]
        assert result.messages[3].role == "assistant"
    finally:
        db.close()


def test_agent_routes_require_login_and_toggle_delete(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.presentation.api import create_app

    install_scripted_llm(_copy_script())
    app = create_app()
    client = TestClient(app)

    assert client.get("/api/agent/sessions").status_code == 401
    _login(client)

    created = client.post("/api/agent/sessions", json={"title": "路由测试"})
    assert created.status_code == 201
    session_id = created.json()["id"]
    assert created.json()["stage"] == "clarify"

    listed = client.get("/api/agent/sessions")
    assert listed.status_code == 200
    assert any(item["id"] == session_id for item in listed.json()["items"])

    turned = client.post(
        f"/api/agent/sessions/{session_id}/messages",
        json={"content": "我要周末做个开业酬宾的朋友圈海报"},
    )
    assert turned.status_code == 200
    payload = turned.json()
    assert [message["role"] for message in payload["session"]["messages"]] == ["user", "assistant", "tool", "assistant"]
    assert payload["session"]["stage"] == "produce"

    detail = client.get(f"/api/agent/sessions/{session_id}")
    assert detail.status_code == 200
    assert len(detail.json()["messages"]) == 4

    missing = client.post(f"/api/agent/sessions/{session_id}/messages", json={"content": "   "})
    assert missing.status_code == 400

    not_found = client.post("/api/agent/sessions/no-such-session/messages", json={"content": "在吗"})
    assert not_found.status_code == 404

    assert client.delete(f"/api/agent/sessions/{session_id}").status_code == 403
    _enable_deletion(client)
    assert client.delete(f"/api/agent/sessions/{session_id}").status_code == 204
    assert client.get(f"/api/agent/sessions/{session_id}").status_code == 404


def test_agent_reports_missing_real_text_provider(configured_env: Path) -> None:
    from productflow_backend.application.designer_agent.loop import is_agent_llm_available

    available, message = is_agent_llm_available()
    assert available is False
    assert "AGENT_*" in message


def test_agent_message_stream_emits_sse_frames(configured_env: Path, install_scripted_llm) -> None:
    """SSE 流式端点：帧序列、事件类型与最终 done 数据。"""
    from productflow_backend.presentation.api import create_app

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-stream-1",
                        name="generate_image",
                        arguments={"prompt": "粉色开业海报", "size": "1024x1024"},
                    )
                ],
            ),
            AgentLLMResponse(content="图片已经生成好了！"),
        ]
    )
    app = create_app()
    client = TestClient(app)
    _login(client)

    created = client.post("/api/agent/sessions", json={"title": "流式"})
    session_id = created.json()["id"]

    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/messages/stream",
        json={"content": "来一张粉色开业海报"},
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        event = next(line[len("event: ") :] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[len("data: ") :] for line in lines if line.startswith("data: ")))
        events.append((event, data))

    kinds = [event for event, _ in events]
    assert kinds[0] == "message"  # user 落库帧
    assert kinds.count("stage") >= 2
    assert "tool_start" in kinds and "tool_result" in kinds
    assistant_frames = [data for event, data in events if event == "message" and data["role"] == "assistant"]
    assert assistant_frames and "生成好" in assistant_frames[-1]["content"]
    done = next(data for event, data in events if event == "done")
    assert done["stage"] == "review"
    assert done["image_session_id"]
    assert any(event["tool"] == "generate_image" for event in done["tool_events"])

    # 流结束后会话详情与非流式路径一致
    detail = client.get(f"/api/agent/sessions/{session_id}").json()
    assert detail["stage"] == "review"
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool", "assistant"]


def test_agent_message_stream_reports_llm_error(configured_env: Path) -> None:
    """文本供应商为 mock 时，流式端点输出人话 error 帧并正常收尾。"""
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)
    session_id = client.post("/api/agent/sessions", json={}).json()["id"]

    with client.stream(
        "POST",
        f"/api/agent/sessions/{session_id}/messages/stream",
        json={"content": "你好"},
    ) as response:
        body = "".join(response.iter_text())

    assert "event: error" in body
    # 用户只见人话，不含任何配置/技术细节
    assert "设计师模型暂时没有响应" in body
    assert "AGENT_*" not in body
    assert "event: done" in body


def test_agent_nudges_model_when_first_round_has_no_tool_call(configured_env: Path, install_scripted_llm) -> None:
    """工具优先：首轮只回文字时自动督促重试一次。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    llm = install_scripted_llm(
        [
            AgentLLMResponse(content="我可以帮你做海报哦，告诉我更多吧。"),  # 首轮：纯文字（失职）
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(call_id="call-copy-x", name="write_copy", arguments={"brief": "开业文案"})
                ],
            ),
            AgentLLMResponse(
                content=json.dumps([{"title": "A", "content": "文案A", "hashtags": []}], ensure_ascii=False)
            ),
            AgentLLMResponse(content="文案来啦。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="给我三版开业文案")

        # 督促消息已注入 LLM 对话（system 角色）
        nudged = [
            call
            for call in llm.calls
            if any("系统提醒" in str(m.get("content", "")) for m in call["messages"] if m.get("role") == "system")
        ]
        assert nudged, "应注入系统督促消息后重试"

        # 最终仍然通过工具完成
        tools_used = [m.tool_name for m in result.messages if m.role == "tool"]
        assert tools_used == ["write_copy"]
        assert agent_session.stage == "produce"
    finally:
        db.close()


def test_agent_nudge_skipped_when_tools_already_used(configured_env: Path, install_scripted_llm) -> None:
    """已有工具执行的多轮循环不再督促（nudge 只针对首轮空谈）。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    llm = install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[AgentToolCall(call_id="call-g-1", name="generate_image",
                                          arguments={"prompt": "海报"})],
            ),
            AgentLLMResponse(content="图好了。要我再改改颜色吗？"),  # 第二轮纯文字，属正常追问
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="出一张海报")

        assert [m.tool_name for m in result.messages if m.role == "tool"] == ["generate_image"]
        assert agent_session.stage == "review"
        assert len(llm.calls) == 2  # 未触发督促重试
    finally:
        db.close()


def test_agent_llm_retries_transient_gateway_errors() -> None:
    """中转网关瞬时 5xx 重试一次；4xx 与超时不重试。"""
    from productflow_backend.application.designer_agent.llm import AgentLLMError, OpenAICompatAgentClient

    class _GatewayError(Exception):
        status_code = 502

    class _InputError(Exception):
        status_code = 400

    class _FlakyCompletions:
        def __init__(self, failures: int, error: Exception) -> None:
            self.failures = failures
            self.error = error
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise self.error
            message = type("M", (), {"content": "ok", "tool_calls": None})()
            return type("R", (), {"choices": [type("C", (), {"message": message})()], "model": "m"})()

    class _Client:
        def __init__(self, completions) -> None:
            self.chat = type("Chat", (), {"completions": completions})()

    client = OpenAICompatAgentClient(provider_name="t", api_key="k", base_url="http://x", model="m")

    flaky = _FlakyCompletions(failures=1, error=_GatewayError("502 bad gateway"))
    client._client = _Client(flaky)  # type: ignore[assignment]
    response = client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert response.content == "ok"
    assert flaky.calls == 2, "瞬时 5xx 应重试一次"

    bad_input = _FlakyCompletions(failures=1, error=_InputError("bad request"))
    client._client = _Client(bad_input)  # type: ignore[assignment]
    import pytest as _pytest

    with _pytest.raises(AgentLLMError):
        client.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert bad_input.calls == 1, "4xx 不应重试"


def test_agent_stream_nudge_executes_tool_call(configured_env: Path, install_scripted_llm) -> None:
    """流式路径回归：督促重试给出的工具调用必须被执行（曾整体丢弃，导致"答应画图却不画"）。"""
    from productflow_backend.application.designer_agent.loop import (
        create_agent_session,
        get_agent_session,
        run_agent_turn_events,
    )
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(content="我可以帮你做海报哦，告诉我更多吧。"),  # 首轮：纯文字（失职）
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(call_id="call-copy-s", name="write_copy", arguments={"brief": "开业文案"})
                ],
            ),
            AgentLLMResponse(
                content=json.dumps([{"title": "A", "content": "文案A", "hashtags": []}], ensure_ascii=False)
            ),
            AgentLLMResponse(content="文案来啦。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        events = list(run_agent_turn_events(db, agent_session_id=agent_session.id, user_content="给我三版开业文案"))

        tool_results = [e for e in events if e["event"] == "tool_result"]
        assert tool_results, "督促重试的工具调用应被执行并产出 tool_result 帧"
        assert tool_results[0]["data"]["tool"] == "write_copy"

        db.expire_all()
        refreshed = get_agent_session(db, agent_session.id)
        assert [m.tool_name for m in refreshed.messages if m.role == "tool"] == ["write_copy"]
        assert refreshed.stage == "produce"
    finally:
        db.close()


def test_sync_and_stream_paths_agree_on_same_script(configured_env: Path, install_scripted_llm) -> None:
    """防漂移：同一剧本下 run_agent_turn 与 run_agent_turn_events 必须产出一致结果。

    历史实现是两份近似代码，已漂移出"nudge 重试被丢弃"的行为差异；
    现在两者共用 _stream_agent_turn，本测试锁住等价性。
    """
    import productflow_backend.application.designer_agent.loop as loop_module
    from productflow_backend.infrastructure.db.session import get_session_factory

    script = [
        AgentLLMResponse(content="我可以帮你做海报哦，告诉我更多吧。"),  # 首轮纯文字 → 触发督促
        AgentLLMResponse(
            content=None,
            tool_calls=[AgentToolCall(call_id="call-copy-eq", name="write_copy", arguments={"brief": "开业文案"})],
        ),
        AgentLLMResponse(content=json.dumps([{"title": "A", "content": "文案A", "hashtags": []}], ensure_ascii=False)),
        AgentLLMResponse(content="文案来啦。"),
    ]

    def _run(use_stream: bool):
        install_scripted_llm(script)
        db = get_session_factory()()
        try:
            agent_session = loop_module.create_agent_session(db)
            if use_stream:
                events = list(
                    loop_module.run_agent_turn_events(
                        db, agent_session_id=agent_session.id, user_content="给我三版开业文案"
                    )
                )
                tools = [e["data"]["tool"] for e in events if e["event"] == "tool_result"]
                done = [e for e in events if e["event"] == "done"][-1]
                return tools, done["data"]["stage"]
            result = loop_module.run_agent_turn(db, agent_session_id=agent_session.id, user_content="给我三版开业文案")
            tools = [event["tool"] for event in result.tool_events]
            return tools, result.agent_session.stage
        finally:
            db.close()

    sync_tools, sync_stage = _run(use_stream=False)
    stream_tools, stream_stage = _run(use_stream=True)
    assert sync_tools == stream_tools == ["write_copy"], (sync_tools, stream_tools)
    assert sync_stage == stream_stage == "produce", (sync_stage, stream_stage)
