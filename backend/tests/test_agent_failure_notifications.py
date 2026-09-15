"""异步生图失败回灌 Agent 会话通知 + LLM 异常文案收敛 + 系统提示词关键规则。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from productflow_backend.application.designer_agent.llm import AgentLLMError, AgentLLMResponse
from productflow_backend.infrastructure.db.models import (
    AgentMessage,
    AgentSession,
    ImageSessionGenerationTask,
)

# --------------------------------------------------------------------------
# 1. agent_notifications：失败通知的发现、去重、轮换与话术安全
# --------------------------------------------------------------------------


def _link_agent_to_image_session(db, agent_session_id: str, image_session_id: str) -> None:
    agent_session = db.get(AgentSession, agent_session_id)
    agent_session.image_session_id = image_session_id
    db.commit()


def _agent_assistant_messages(db, agent_session_id: str) -> list[str]:
    return list(
        db.scalars(
            select(AgentMessage.content)
            .where(AgentMessage.session_id == agent_session_id, AgentMessage.role == "assistant")
            .order_by(AgentMessage.created_at, AgentMessage.id)
        ).all()
    )


def _add_tool_message(db, agent_session_id: str, image_session_id: str) -> None:
    """模拟 agent 工具消息留下的 image_session 关联（反查路径）。"""
    db.add(
        AgentMessage(
            session_id=agent_session_id,
            role="tool",
            content=json.dumps({"image_session_id": image_session_id}),
            tool_call_id="call-notify",
            tool_name="generate_image",
            image_session_id=image_session_id,
        )
    )
    db.commit()


def test_notify_failure_writes_plain_language_message_without_technical_details(configured_env: Path) -> None:
    from productflow_backend.application.agent_notifications import (
        FAILURE_NOTICE_TEMPLATES,
        notify_agent_session_of_failure,
    )
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import create_image_session
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        image_session = create_image_session(db, title="失败回灌")
        agent_session = create_agent_session(db, title="失败回灌会话")
        _link_agent_to_image_session(db, agent_session.id, image_session.id)

        raw_reason = "Cloudflare 1033 ray-id=abcdef https://gateway/upstream sk-test-should-not-leak"
        notified = notify_agent_session_of_failure(db, image_session.id, raw_reason, "image_generation")

        assert notified == [agent_session.id]
        contents = _agent_assistant_messages(db, agent_session.id)
        assert len(contents) == 1
        assert contents[0] in FAILURE_NOTICE_TEMPLATES
        assert "重试" in contents[0]
        # 技术细节一个字都不许出现
        assert "Cloudflare" not in contents[0]
        assert "ray-id" not in contents[0]
        assert "sk-test" not in contents[0]
        assert "https://" not in contents[0]
    finally:
        db.close()


def test_notify_failure_dedupes_within_same_day_and_rotates_across_sessions(configured_env: Path) -> None:
    from productflow_backend.application.agent_notifications import (
        FAILURE_NOTICE_TEMPLATES,
        notify_agent_session_of_failure,
    )
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import create_image_session
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        image_session = create_image_session(db, title="去重")
        agent_session = create_agent_session(db, title="去重会话")
        _link_agent_to_image_session(db, agent_session.id, image_session.id)

        first = notify_agent_session_of_failure(db, image_session.id, "第一次失败", "image_generation")
        second = notify_agent_session_of_failure(db, image_session.id, "第二次失败", "image_generation")

        assert first == [agent_session.id]
        assert second == [], "同一天同一会话同一 image_session 只通知一次"
        contents = _agent_assistant_messages(db, agent_session.id)
        assert contents == [FAILURE_NOTICE_TEMPLATES[0]]

        # 另一个 image_session 失败：允许再通知，且话术轮换（不与上一条一字不差）
        other_image_session = create_image_session(db, title="轮换")
        _add_tool_message(db, agent_session.id, other_image_session.id)

        third = notify_agent_session_of_failure(db, other_image_session.id, "第三次失败", "image_generation")
        assert third == [agent_session.id]
        contents = _agent_assistant_messages(db, agent_session.id)
        assert len(contents) == 2
        assert len(set(contents)) == 2, "连续失败话术必须轮换"
    finally:
        db.close()


def test_notify_failure_reverse_lookup_via_messages_and_silent_skip(configured_env: Path) -> None:
    from productflow_backend.application.agent_notifications import notify_agent_session_of_failure
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import create_image_session
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        # agent_sessions.image_session_id 为空，只能靠 agent_messages.image_session_id 反查
        image_session = create_image_session(db, title="反查")
        agent_session = create_agent_session(db, title="反查会话")
        _add_tool_message(db, agent_session.id, image_session.id)

        notified = notify_agent_session_of_failure(db, image_session.id, "网关超时", "image_generation")
        assert notified == [agent_session.id]
        assert len(_agent_assistant_messages(db, agent_session.id)) == 1

        # 无任何关联会话：静默跳过，不报错、不落任何消息
        orphan_image_session = create_image_session(db, title="无关联")
        total_before = db.scalar(select(func.count(AgentMessage.id)))
        notified = notify_agent_session_of_failure(db, orphan_image_session.id, "无关联失败", "image_generation")
        assert notified == []
        total_after = db.scalar(select(func.count(AgentMessage.id)))
        assert total_before == total_after

        # 空 image_session_id 同样静默跳过
        assert notify_agent_session_of_failure(db, "", "空", "image_generation") == []
    finally:
        db.close()


def test_worker_terminal_failure_notifies_linked_agent_session(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """worker 重试耗尽后的终态失败必须回灌 agent 会话，且只通知一次。"""
    from productflow_backend.application.agent_notifications import FAILURE_NOTICE_TEMPLATES
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import (
        IMAGE_SESSION_GENERATION_MAX_ATTEMPTS,
        create_image_session,
        create_image_session_generation_task,
        execute_image_session_generation_task,
    )
    from productflow_backend.domain.enums import JobStatus

    calls = {"count": 0}

    def fail_generate(*args, **kwargs) -> None:
        calls["count"] += 1
        raise RuntimeError("provider raw secret sk-test path=/tmp/provider-traceback")

    monkeypatch.setattr(
        "productflow_backend.infrastructure.image.chat_service.ImageChatService.generate",
        fail_generate,
    )

    image_session = create_image_session(db_session, title="worker 终态失败")
    agent_session = create_agent_session(db_session, title="worker 终态失败会话")
    _link_agent_to_image_session(db_session, agent_session.id, image_session.id)
    result = create_image_session_generation_task(
        db_session,
        image_session_id=image_session.id,
        prompt="这批海报会失败",
        size="1024x1024",
    )

    execute_image_session_generation_task(result.task.id)

    assert calls["count"] == IMAGE_SESSION_GENERATION_MAX_ATTEMPTS
    db_session.expire_all()
    task = db_session.get(ImageSessionGenerationTask, result.task.id)
    assert task is not None
    assert task.status == JobStatus.FAILED
    assert task.attempts == IMAGE_SESSION_GENERATION_MAX_ATTEMPTS

    contents = _agent_assistant_messages(db_session, agent_session.id)
    assert len(contents) == 1, "重试耗尽只回灌一次失败通知"
    assert contents[0] in FAILURE_NOTICE_TEMPLATES
    assert "重试" in contents[0]
    assert "sk-test" not in contents[0]


def test_worker_non_retryable_failure_notifies_linked_agent_session(
    configured_env: Path, db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """不可重试的策略拒绝同样属于终态失败，需要立即回灌。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import (
        create_image_session,
        create_image_session_generation_task,
        execute_image_session_generation_task,
    )

    def fail_generate(*args, **kwargs) -> None:
        raise RuntimeError("Request blocked by content policy")

    monkeypatch.setattr(
        "productflow_backend.infrastructure.image.chat_service.ImageChatService.generate",
        fail_generate,
    )

    image_session = create_image_session(db_session, title="策略拒绝回灌")
    agent_session = create_agent_session(db_session, title="策略拒绝回灌会话")
    _link_agent_to_image_session(db_session, agent_session.id, image_session.id)
    result = create_image_session_generation_task(
        db_session,
        image_session_id=image_session.id,
        prompt="策略拒绝立即回灌",
        size="1024x1024",
    )

    execute_image_session_generation_task(result.task.id)

    db_session.expire_all()
    contents = _agent_assistant_messages(db_session, agent_session.id)
    assert len(contents) == 1
    assert "重试" in contents[0]


# --------------------------------------------------------------------------
# 2. loop.py：LLM 异常文案收敛（不含原始异常、连续失败轮换）
# --------------------------------------------------------------------------


class _FailingLLM:
    provider_name = "scripted"
    model = "scripted-model"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, *, messages, tools, intent: str = "") -> AgentLLMResponse:
        self.calls += 1
        raise AgentLLMError("Cloudflare error 1033 ray-id=deadbeef https://gateway/upstream")


def test_agent_turn_llm_error_persists_plain_language_and_rotates(configured_env: Path) -> None:
    from productflow_backend.application.designer_agent.loop import (
        _LLM_FAILURE_TEMPLATES,
        create_agent_session,
        run_agent_turn,
    )
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    failing = _FailingLLM()
    try:
        agent_session = create_agent_session(db)
        with pytest.raises(AgentLLMError):
            run_agent_turn(db, agent_session_id=agent_session.id, user_content="第一条", llm=failing)
        with pytest.raises(AgentLLMError):
            run_agent_turn(db, agent_session_id=agent_session.id, user_content="第二条", llm=failing)

        contents = _agent_assistant_messages(db, agent_session.id)
        assert len(contents) == 2
        for content in contents:
            assert content in _LLM_FAILURE_TEMPLATES
            assert "Cloudflare" not in content
            assert "ray-id" not in content
            assert "https://" not in content
        assert contents[0] != contents[1], "连续失败的同文案必须轮换"
    finally:
        db.close()


def test_agent_turn_events_llm_error_frame_is_plain_language(configured_env: Path) -> None:
    from productflow_backend.application.designer_agent.loop import (
        _LLM_FAILURE_TEMPLATES,
        create_agent_session,
        run_agent_turn_events,
    )
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        events = list(
            run_agent_turn_events(db, agent_session_id=agent_session.id, user_content="在吗", llm=_FailingLLM())
        )

        error_frames = [event for event in events if event["event"] == "error"]
        assert len(error_frames) == 1
        message = error_frames[0]["data"]["message"]
        assert message in _LLM_FAILURE_TEMPLATES
        assert "Cloudflare" not in message and "https://" not in message
        assert any(event["event"] == "done" for event in events)

        # 落库消息与 error 帧一致，均为人话
        contents = _agent_assistant_messages(db, agent_session.id)
        assert contents == [message]
    finally:
        db.close()


# --------------------------------------------------------------------------
# 3. prompts.py：体验规则融合进系统提示词
# --------------------------------------------------------------------------


def test_agent_system_prompt_contains_experience_rules() -> None:
    from productflow_backend.application.designer_agent.prompts import AGENT_SYSTEM_PROMPT

    for phrase in (
        "先后顺序",  # 规则 1：第一句回应最新消息，未完成任务先排序
        "completed_assets",  # 规则 2：只有工具结果列出 completed_assets/下载链接才说图已出来
        "正在生成",
        "save_asset",  # 规则 3：动作不偷换
        "write_copy_report",
        "不许再问",  # 规则 4：已答不再问、未答按默认值推进
        "默认值推进",
        "严格按那个数量出",  # 规则 5：用户指定数量就按数量出
        "第一句先认错",  # 规则 6：失败认错 + 两条路，技术细节与错误码不出现
        "重试一次",
        "换个风格再来一版",
        "错误码",
        "收尾语雷同",  # 规则 7：收尾语最多说一次
    ):
        assert phrase in AGENT_SYSTEM_PROMPT, f"系统提示词缺少关键规则短语: {phrase}"


def test_enqueue_failed_marks_failed_and_notifies_linked_agent_session(
    configured_env: Path, db_session
) -> None:
    """入队即失败（API 侧投递不出去）也要落终态并回灌 agent 会话。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session
    from productflow_backend.application.image_sessions import (
        create_image_session,
        create_image_session_generation_task,
        mark_image_session_generation_task_enqueue_failed,
    )
    from productflow_backend.domain.enums import JobStatus

    image_session = create_image_session(db_session, title="入队失败")
    agent_session = create_agent_session(db_session, title="入队失败会话")
    _link_agent_to_image_session(db_session, agent_session.id, image_session.id)
    result = create_image_session_generation_task(
        db_session,
        image_session_id=image_session.id,
        prompt="这批海报进不了队列",
        size="1024x1024",
    )

    mark_image_session_generation_task_enqueue_failed(
        db_session, task_id=result.task.id, reason="Redis connection refused to 127.0.0.1:16379"
    )

    db_session.expire_all()
    task = db_session.get(ImageSessionGenerationTask, result.task.id)
    assert task is not None
    assert task.status == JobStatus.FAILED
    assert task.progress_phase == "enqueue_failed"

    contents = _agent_assistant_messages(db_session, agent_session.id)
    assert len(contents) == 1, "入队失败回灌一次人话通知"
    assert "重试" in contents[0]
    assert "Redis" not in contents[0] and "16379" not in contents[0]
