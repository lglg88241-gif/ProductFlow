from __future__ import annotations

import json
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

MAX_TOOL_ROUNDS = 8
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
    for message in agent_session.messages:
        messages.append(_message_to_llm_format(message))
    return messages


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
                result = execute_tool(db, agent_session, name=call.name, arguments=call.arguments, llm=client)
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
