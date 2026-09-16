from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from productflow_backend.application.designer_agent.llm import AgentLLMClient, AgentLLMError, build_agent_llm_client
from productflow_backend.application.designer_agent.prompts import AGENT_SYSTEM_PROMPT, DEFAULT_STAGE, STAGE_BY_TOOL
from productflow_backend.application.designer_agent.tools import execute_tool, tool_schemas
from productflow_backend.application.isolation import ensure_row_readable, owner_filter_expression
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

# LLM 异常的用户可见文案：3 条措辞不同、按会话内失败次数轮换，防止同文案刷屏。
# 原始异常（含网关/Cloudflare JSON）只进日志，绝不拼进用户文本。
_LLM_FAILURE_TEMPLATES: tuple[str, str, str] = (
    "设计模型这会儿连不上，刚才那条没处理成。稍后再发一次，我马上接着干。",
    "刚和设计大脑的连接断了一下，这一轮没能走完。请把刚才的话再发一次试试。",
    "模型服务临时抽风了，这条请求没能完成。缓一缓再重发，我随时在。",
)


def _llm_failure_text(db: Session, agent_session_id: str) -> str:
    """按会话内已落库的失败文案条数轮换模板，连续失败不再一字不差地重复。"""
    try:
        prior = db.scalar(
            select(func.count(AgentMessage.id)).where(
                AgentMessage.session_id == agent_session_id,
                AgentMessage.role == "assistant",
                AgentMessage.content.in_(_LLM_FAILURE_TEMPLATES),
            )
        )
    except SQLAlchemyError:
        prior = 0
    return _LLM_FAILURE_TEMPLATES[(prior or 0) % len(_LLM_FAILURE_TEMPLATES)]


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    agent_session: AgentSession
    messages: list[AgentMessage]
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    pending_generation_tasks: list[dict[str, Any]] = field(default_factory=list)


def create_agent_session(
    db: Session, *, title: str | None = None, owner_id: str | None = None
) -> AgentSession:
    normalized = (title or "").strip() or _DEFAULT_SESSION_TITLE
    agent_session = AgentSession(title=normalized[:120], stage=DEFAULT_STAGE, owner_id=owner_id)
    db.add(agent_session)
    db.commit()
    db.refresh(agent_session)
    return agent_session


def list_agent_sessions(db: Session, owner_id: str | None = None) -> list[AgentSession]:
    """列出会话；owner_id 非 None（隔离开启）时只返回自己的 + 全局可读的。"""
    statement = select(AgentSession).order_by(AgentSession.updated_at.desc(), AgentSession.id)
    owner_clause = owner_filter_expression(AgentSession.owner_id, owner_id)
    if owner_clause is not None:
        statement = statement.where(owner_clause)
    return list(db.scalars(statement).all())


def get_agent_session(db: Session, agent_session_id: str, owner_id: str | None = None) -> AgentSession:
    """取会话并做归属判定：跨用户访问与"不存在"同文案（不泄漏存在性）。"""
    agent_session = db.scalar(
        select(AgentSession)
        .options(selectinload(AgentSession.messages))
        .where(AgentSession.id == agent_session_id)
    )
    if agent_session is None:
        raise NotFoundError("设计师会话不存在")
    ensure_row_readable(agent_session, owner_id, message="设计师会话不存在")
    return agent_session


def delete_agent_session(db: Session, agent_session_id: str, owner_id: str | None = None) -> None:
    agent_session = db.get(AgentSession, agent_session_id)
    if agent_session is None:
        raise NotFoundError("设计师会话不存在")
    ensure_row_readable(agent_session, owner_id, message="设计师会话不存在")
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
    # 裁剪按"完整工具组"做，而不是"从首个工具调用开始截"。
    #
    # 历史缺陷（审计 R4）：起点曾被设为第一个带 tool_calls 的 assistant，导致它之前
    # 的全部消息被丢弃——用户最初的需求就此消失，4 条消息的短会话也会丢需求，
    # 模型于是以为用户什么都没说过。正确做法是保留窗口内的顺序上下文，
    # 只丢弃"不完整"的工具组及其孤立结果（断线产物与截断产物，二者发给
    # OpenAI 兼容 API 都会 400）。
    paired_tool_call_ids = {
        message.tool_call_id for message in recent if message.role == "tool" and message.tool_call_id
    }
    kept_group_call_ids: set[str] = set()
    for message in recent:
        if message.role == "assistant" and message.tool_calls_json:
            group_call_ids = {call.get("call_id", "") for call in message.tool_calls_json}
            if group_call_ids - paired_tool_call_ids:
                # 组内存在无配对结果的 call → 整组跳过（连带其残缺 tool 结果）
                continue
            kept_group_call_ids |= group_call_ids
            messages.append(_message_to_llm_format(message))
        elif message.role == "tool":
            # tool 结果只有在其 assistant 组先被保留时才可用；孤立结果一律丢弃
            if message.tool_call_id not in kept_group_call_ids:
                continue
            messages.append(_message_to_llm_format(message))
        else:
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


@dataclass
class _TurnState:
    """一轮对话的跨事件状态：核心生成器写入，两种消费方式（流式/同步）各自取用。

    把状态放在显式对象里，是为了让 run_agent_turn 与 run_agent_turn_events 共用同一份
    循环逻辑——历史实现是两份近似代码，已经漂移出行为差异（nudge 重试被丢弃）。
    """

    tool_events: list[dict[str, Any]] = field(default_factory=list)
    pending_generation_tasks: list[dict[str, Any]] = field(default_factory=list)
    llm_error: AgentLLMError | None = None
    final_session: AgentSession | None = None


def run_agent_turn(
    db: Session,
    *,
    agent_session_id: str,
    user_content: str,
    llm: AgentLLMClient | None = None,
) -> AgentTurnResult:
    """执行一轮对话：持久化用户消息 → LLM 工具循环 → 最终回复落库。

    实现上消费事件化核心 `_stream_agent_turn`（唯一一份循环逻辑），本函数只做
    "驱动生成器 + 组装返回值"的适配。LLM 连接失败时保持历史语义：失败话术已落库，
    并向上抛出 AgentLLMError 供路由转 503。
    """
    state = _TurnState()
    for _event in _stream_agent_turn(
        db,
        agent_session_id=agent_session_id,
        user_content=user_content,
        llm=llm,
        state=state,
    ):
        pass
    if state.llm_error is not None:
        raise state.llm_error
    refreshed = state.final_session or get_agent_session(db, agent_session_id)
    return AgentTurnResult(
        agent_session=refreshed,
        messages=list(refreshed.messages),
        tool_events=state.tool_events,
        pending_generation_tasks=state.pending_generation_tasks,
    )


def append_agent_context_note(db: Session, *, agent_session_id: str, content: str) -> AgentMessage:
    """往会话里追加一条"系统告知"式的上下文（以 user 角色承载，UI 与模型都可见）。

    用于把不经过模型的外部事件（如用户刚上传了模板）告知 agent，
    使下一轮对话不必用户复述就能引用它。
    """
    agent_session = get_agent_session(db, agent_session_id)
    return _persist_message(db, agent_session, role="user", content=content)


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
    """事件化的一轮对话（公开入口）：每个关键步骤即时 yield，供 SSE 流式输出。

    帧类型：
      stage      — 阶段徽章变化（session 级）
      message    — 一条已落库消息（user/assistant）
      tool_start — 工具开始执行（前端显示"正在生成…"）
      tool_result— 工具结果（文案提案/图片资产/错误话术）
      done       — 整轮结束，携带与会话快照等价的最小结果
      error      — LLM 连接失败等人话错误（随后 done）

    同步形态的 run_agent_turn 消费同一个核心生成器，两条路径不会漂移。
    """
    yield from _stream_agent_turn(
        db,
        agent_session_id=agent_session_id,
        user_content=user_content,
        llm=llm,
        state=_TurnState(),
    )


def _stream_agent_turn(
    db: Session,
    *,
    agent_session_id: str,
    user_content: str,
    llm: AgentLLMClient | None = None,
    state: _TurnState,
) -> Generator[dict[str, Any], None, None]:
    """一轮对话的唯一核心实现：落库 + LLM 工具循环 + 事件产出。

    结果同时写入 `state`（工具事件、待跟踪生成任务、LLM 异常、最终会话快照），
    使同步调用方无需解析事件流即可拿到等价结果。
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
    used_tools: list[str] = []

    def _finish() -> Generator[dict[str, Any], None, None]:
        db.expire_all()
        refreshed = get_agent_session(db, agent_session_id)
        state.final_session = refreshed
        yield {"event": "stage", "data": {"stage": refreshed.stage}}
        yield {
            "event": "done",
            "data": {
                "session_id": refreshed.id,
                "stage": refreshed.stage,
                "image_session_id": refreshed.image_session_id,
                "tool_events": state.tool_events,
                "pending_generation_tasks": state.pending_generation_tasks,
            },
        }

    tool_nudged = False
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            response = client.chat(messages=llm_messages, tools=tool_schemas())
        except AgentLLMError as exc:
            logger.exception("Agent LLM 调用失败(流式): session_id=%s", agent_session_id)
            state.llm_error = exc
            failure_text = _llm_failure_text(db, agent_session.id)
            _persist_message(db, agent_session, role="assistant", content=failure_text)
            db.expire_all()
            yield {"event": "error", "data": {"message": failure_text}}
            yield from _finish()
            return
        if not response.tool_calls and not tool_nudged and not used_tools:
            # 首轮只回文字未调工具：注入督促后重试一次（工具优先铁律）。
            # 督促重试若给出工具调用，会落入下方 tool_calls 分支正常执行。
            tool_nudged = True
            llm_messages.append({"role": "system", "content": _TOOL_NUDGE_MESSAGE})
            try:
                response = client.chat(messages=llm_messages, tools=tool_schemas())
            except AgentLLMError as exc:
                logger.exception("Agent LLM 调用失败(流式·督促重试): session_id=%s", agent_session_id)
                state.llm_error = exc
                failure_text = _llm_failure_text(db, agent_session.id)
                _persist_message(db, agent_session, role="assistant", content=failure_text)
                db.expire_all()
                yield {"event": "error", "data": {"message": failure_text}}
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
                state.tool_events.append({"tool": call.name, "result": result})
                yield {"event": "tool_result", "data": {"tool": call.name, "result": result}}
                for task in result.get("pending_tasks", []) or []:
                    state.pending_generation_tasks.append(
                        {"image_session_id": result.get("image_session_id"), **task}
                    )
            continue

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
