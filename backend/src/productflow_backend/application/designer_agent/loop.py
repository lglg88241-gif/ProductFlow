from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from productflow_backend.application.designer_agent.llm import AgentLLMClient, AgentLLMError, build_agent_llm_client
from productflow_backend.application.designer_agent.prompts import AGENT_SYSTEM_PROMPT, DEFAULT_STAGE, STAGE_BY_TOOL
from productflow_backend.application.designer_agent.tools import execute_tool, tool_schemas
from productflow_backend.domain.errors import NotFoundError
from productflow_backend.infrastructure.db.models import AgentMessage, AgentSession

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
# 首轮模型只回文字未调工具时的督促重试（一次），强制"工具优先"
_TOOL_NUDGE_MESSAGE = (
    "（系统提醒）用户在等待实际产出。请立即调用合适的工具完成请求"
    "（文案用 write_copy、图用 generate_image/edit_image、方案用 recommend_designs），"
    "不要只用文字回复。"
)
# 引导对话只需近期上下文；历史截断控制 token 成本与轮次延迟
MAX_HISTORY_MESSAGES = 20
_DEFAULT_SESSION_TITLE = "设计师会话"


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    agent_session: AgentSession
    messages: list[AgentMessage]
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    pending_generation_tasks: list[dict[str, Any]] = field(default_factory=list)


def create_agent_session(db: Session, *, title: str | None = None) -> AgentSession:
    normalized = (title or "").strip() or _DEFAULT_SESSION_TITLE
    agent_session = AgentSession(title=normalized[:120], stage=DEFAULT_STAGE)
    db.add(agent_session)
    db.commit()
    db.refresh(agent_session)
    return agent_session


def list_agent_sessions(db: Session) -> list[AgentSession]:
    return list(
        db.scalars(select(AgentSession).order_by(AgentSession.updated_at.desc(), AgentSession.id)).all()
    )


def get_agent_session(db: Session, agent_session_id: str) -> AgentSession:
    agent_session = db.scalar(
        select(AgentSession)
        .options(selectinload(AgentSession.messages))
        .where(AgentSession.id == agent_session_id)
    )
    if agent_session is None:
        raise NotFoundError("设计师会话不存在")
    return agent_session


def delete_agent_session(db: Session, agent_session_id: str) -> None:
    agent_session = db.get(AgentSession, agent_session_id)
    if agent_session is None:
        raise NotFoundError("设计师会话不存在")
    db.delete(agent_session)
    db.commit()


def _message_to_llm_format(message: AgentMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {"role": "tool", "tool_call_id": message.tool_call_id or "", "content": message.content}
    if message.role == "assistant" and message.tool_calls_json:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.get("call_id", ""),
                    "type": "function",
                    "function": {
                        "name": call.get("name", ""),
                        "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                    },
                }
                for call in message.tool_calls_json
            ],
        }
    return {"role": message.role, "content": message.content}


def _build_llm_messages(agent_session: AgentSession) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    recent = list(agent_session.messages)[-MAX_HISTORY_MESSAGES:]
    # 截断不能把 assistant 的 tool_calls 与对应 tool 结果拆开：从首个完整组开始
    start = 0
    for index, message in enumerate(recent):
        if message.role == "assistant" and message.tool_calls_json:
            start = index
            break
    # 孤儿 tool_calls 自愈：SSE 断线可能留下 assistant(tool_calls) 已落库、tool 结果未落库的
    # 半截组，下一轮发给 OpenAI 兼容 API 会直接 400 且每轮复现。构建时校验配对：存在无配对
    # 结果 call 的 assistant 组整组跳过（连带其残缺 tool 结果）；无状态过滤、不改库。
    paired_tool_call_ids = {
        message.tool_call_id for message in recent if message.role == "tool" and message.tool_call_id
    }
    skip_group = True  # 组前的孤立 tool 消息（截断产物）同样不放行
    for message in recent[start:]:
        if message.role == "assistant" and message.tool_calls_json:
            skip_group = any(
                call.get("call_id", "") not in paired_tool_call_ids for call in message.tool_calls_json
            )
            if skip_group:
                continue
        elif message.role == "tool" and skip_group:
            continue
        messages.append(_message_to_llm_format(message))
    return messages


def _usage_columns(usage: dict[str, Any] | None) -> dict[str, Any]:
    """把 LLM 响应的 usage 摘要映射为 AgentMessage 的 token 落库字段（无 usage 时不落）。"""
    if not usage:
        return {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
    }


def _persist_message(db: Session, agent_session: AgentSession, **kwargs: Any) -> AgentMessage:
    message = AgentMessage(session_id=agent_session.id, **kwargs)
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def run_agent_turn(
    db: Session,
    *,
    agent_session_id: str,
    user_content: str,
    llm: AgentLLMClient | None = None,
) -> AgentTurnResult:
    """执行一轮对话：持久化用户消息 → LLM 工具循环 → 最终回复落库。"""
    agent_session = get_agent_session(db, agent_session_id)
    normalized_content = user_content.strip()
    if not normalized_content:
        raise ValueError("消息内容不能为空")
    _persist_message(db, agent_session, role="user", content=normalized_content)

    client = llm or build_agent_llm_client()
    llm_messages = _build_llm_messages(agent_session)
    tool_events: list[dict[str, Any]] = []
    pending_generation_tasks: list[dict[str, Any]] = []
    used_tools: list[str] = []

    tool_nudged = False
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat(messages=llm_messages, tools=tool_schemas())
        except AgentLLMError as exc:
            _persist_message(
                db,
                agent_session,
                role="assistant",
                content=f"我这边连接设计模型时遇到了问题，请稍后再试一次。（{exc}）",
            )
            raise
        if not response.tool_calls and not tool_nudged and not used_tools:
            tool_nudged = True
            llm_messages.append({"role": "system", "content": _TOOL_NUDGE_MESSAGE})
            response = client.chat(messages=llm_messages, tools=tool_schemas())
        if response.tool_calls:
            _persist_message(
                db,
                agent_session,
                role="assistant",
                content=response.content or "",
                tool_calls_json=[
                    {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
                **_usage_columns(response.usage),
            )
            llm_messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                tool_started = time.perf_counter()
                result = execute_tool(db, agent_session, name=call.name, arguments=call.arguments, llm=client)
                logger.info(
                    "Agent 工具执行完成: tool=%s duration_ms=%.0f",
                    call.name,
                    (time.perf_counter() - tool_started) * 1000,
                )
                used_tools.append(call.name)
                result_json = json.dumps(result, ensure_ascii=False)
                _persist_message(
                    db,
                    agent_session,
                    role="tool",
                    content=result_json,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    image_session_id=result.get("image_session_id"),
                )
                llm_messages.append({"role": "tool", "tool_call_id": call.call_id, "content": result_json})
                tool_events.append({"tool": call.name, "result": result})
                for task in result.get("pending_tasks", []) or []:
                    pending_generation_tasks.append({"image_session_id": result.get("image_session_id"), **task})
            continue

        _persist_message(
            db,
            agent_session,
            role="assistant",
            content=(response.content or "").strip() or "（我没想到要说什么，请再告诉我一点需求。）",
            **_usage_columns(response.usage),
        )
        _advance_stage(db, agent_session, used_tools)
        db.expire_all()
        refreshed = get_agent_session(db, agent_session_id)
        return AgentTurnResult(
            agent_session=refreshed,
            messages=list(refreshed.messages),
            tool_events=tool_events,
            pending_generation_tasks=pending_generation_tasks,
        )

    _persist_message(
        db,
        agent_session,
        role="assistant",
        content="这个需求比预想的复杂，我们先分解一下：你希望我先出文案，还是先看几张风格参考？",
    )
    db.expire_all()
    refreshed = get_agent_session(db, agent_session_id)
    return AgentTurnResult(
        agent_session=refreshed,
        messages=list(refreshed.messages),
        tool_events=tool_events,
        pending_generation_tasks=pending_generation_tasks,
    )


def _advance_stage(db: Session, agent_session: AgentSession, used_tools: list[str]) -> None:
    for tool_name in reversed(used_tools):
        stage = STAGE_BY_TOOL.get(tool_name)
        if stage:
            agent_session.stage = stage
            db.commit()
            return


def is_agent_llm_available() -> tuple[bool, str]:
    try:
        build_agent_llm_client()
    except (AgentLLMError, SQLAlchemyError) as exc:
        return False, str(exc)
    return True, ""


def run_agent_turn_events(
    db: Session,
    *,
    agent_session_id: str,
    user_content: str,
    llm: AgentLLMClient | None = None,
) -> Generator[dict[str, Any], None, None]:
    """事件化的一轮对话：每个关键步骤即时 yield，供 SSE 流式输出。

    帧类型：
      stage      — 阶段徽章变化（session 级）
      message    — 一条已落库消息（user/assistant）
      tool_start — 工具开始执行（前端显示"正在生成…"）
      tool_result— 工具结果（文案提案/图片资产/错误话术）
      done       — 整轮结束，携带与会话快照等价的最小结果
      error      — LLM 连接失败等人话错误（随后 done）
    """
    agent_session = get_agent_session(db, agent_session_id)
    normalized_content = user_content.strip()
    if not normalized_content:
        raise ValueError("消息内容不能为空")
    user_message = _persist_message(db, agent_session, role="user", content=normalized_content)
    yield {
        "event": "message",
        "data": {"id": user_message.id, "role": "user", "content": user_message.content},
    }
    yield {"event": "stage", "data": {"stage": agent_session.stage}}

    client = llm or build_agent_llm_client()
    llm_messages = _build_llm_messages(agent_session)
    tool_events: list[dict[str, Any]] = []
    pending_generation_tasks: list[dict[str, Any]] = []
    used_tools: list[str] = []

    def _finish() -> Generator[dict[str, Any], None, None]:
        db.expire_all()
        refreshed = get_agent_session(db, agent_session_id)
        yield {"event": "stage", "data": {"stage": refreshed.stage}}
        yield {
            "event": "done",
            "data": {
                "session_id": refreshed.id,
                "stage": refreshed.stage,
                "image_session_id": refreshed.image_session_id,
                "tool_events": tool_events,
                "pending_generation_tasks": pending_generation_tasks,
            },
        }

    tool_nudged = False
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat(messages=llm_messages, tools=tool_schemas())
        except AgentLLMError as exc:
            _persist_message(
                db,
                agent_session,
                role="assistant",
                content=f"我这边连接设计模型时遇到了问题，请稍后再试一次。（{exc}）",
            )
            db.expire_all()
            yield {"event": "error", "data": {"message": str(exc)}}
            yield from _finish()
            return
        if response.tool_calls:
            _persist_message(
                db,
                agent_session,
                role="assistant",
                content=response.content or "",
                tool_calls_json=[
                    {"call_id": call.call_id, "name": call.name, "arguments": call.arguments}
                    for call in response.tool_calls
                ],
                **_usage_columns(response.usage),
            )
            llm_messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in response.tool_calls
                    ],
                }
            )
            for call in response.tool_calls:
                yield {"event": "tool_start", "data": {"tool": call.name}}
                tool_started = time.perf_counter()
                result = execute_tool(db, agent_session, name=call.name, arguments=call.arguments, llm=client)
                logger.info(
                    "Agent 工具执行完成: tool=%s duration_ms=%.0f",
                    call.name,
                    (time.perf_counter() - tool_started) * 1000,
                )
                used_tools.append(call.name)
                result_json = json.dumps(result, ensure_ascii=False)
                _persist_message(
                    db,
                    agent_session,
                    role="tool",
                    content=result_json,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    image_session_id=result.get("image_session_id"),
                )
                llm_messages.append({"role": "tool", "tool_call_id": call.call_id, "content": result_json})
                tool_events.append({"tool": call.name, "result": result})
                yield {"event": "tool_result", "data": {"tool": call.name, "result": result}}
                for task in result.get("pending_tasks", []) or []:
                    pending_generation_tasks.append({"image_session_id": result.get("image_session_id"), **task})
            continue

        if not tool_nudged and not used_tools:
            # 首轮只回文字未调工具：注入督促后重试一次（工具优先铁律）
            tool_nudged = True
            llm_messages.append({"role": "system", "content": _TOOL_NUDGE_MESSAGE})
            try:
                response = client.chat(messages=llm_messages, tools=tool_schemas())
            except AgentLLMError as exc:
                _persist_message(
                    db,
                    agent_session,
                    role="assistant",
                    content=f"我这边连接设计模型时遇到了问题，请稍后再试一次。（{exc}）",
                )
                db.expire_all()
                yield {"event": "error", "data": {"message": str(exc)}}
                yield from _finish()
                return
        assistant_message = _persist_message(
            db,
            agent_session,
            role="assistant",
            content=(response.content or "").strip() or "（我没想到要说什么，请再告诉我一点需求。）",
            **_usage_columns(response.usage),
        )
        yield {
            "event": "message",
            "data": {"id": assistant_message.id, "role": "assistant", "content": assistant_message.content},
        }
        _advance_stage(db, agent_session, used_tools)
        yield from _finish()
        return

    fallback = _persist_message(
        db,
        agent_session,
        role="assistant",
        content="这个需求比预想的复杂，我们先分解一下：你希望我先出文案，还是先看几张风格参考？",
    )
    yield {
        "event": "message",
        "data": {"id": fallback.id, "role": "assistant", "content": fallback.content},
    }
    yield from _finish()
