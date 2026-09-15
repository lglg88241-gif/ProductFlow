"""生图任务终态失败 → 回灌 Agent 会话的用户可见通知。

只 import DB models，绝不 import designer_agent/tools/image_sessions（避免循环导入）。
落库文案全部是给人看的大白话：不含 failure_reason 原文、错误码、URL 等技术细节，
原始失败原因只进日志。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from productflow_backend.infrastructure.db.models import AgentMessage, AgentSession

logger = logging.getLogger(__name__)

# 3 条话术轮换，防止连续失败时同一句刷屏；每条都给出"重试 / 换风格"两条路。
FAILURE_NOTICE_TEMPLATES: tuple[str, ...] = (
    "刚那批海报没能生成出来，是绘图服务那边抽风了。想让我原地重试一次，还是换个风格再来一版？",
    "这批图这轮没能出出来，绘图服务临时闹了点小脾气。要我直接重试一遍，还是换个风格再来一版？",
    "海报没能按计划生成出来，绘图服务这会儿不太稳定。可以让我重试一次，或者换个风格重新做一版。",
)


def _agent_session_ids_for_image_session(db: Session, image_session_id: str) -> list[str]:
    """找关联 agent 会话：agent_sessions.image_session_id 直连 + agent_messages 反查，去重保序。"""
    direct = set(
        db.scalars(select(AgentSession.id).where(AgentSession.image_session_id == image_session_id)).all()
    )
    via_messages = set(
        db.scalars(
            select(AgentMessage.session_id).where(AgentMessage.image_session_id == image_session_id)
        ).all()
    )
    return sorted(direct | via_messages)


def _failure_notice_template_for(db: Session, agent_session_id: str) -> str:
    """按会话内已有失败通知条数轮换文案，连续失败不再一字不差地重复。"""
    prior = db.scalar(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.session_id == agent_session_id,
            AgentMessage.role == "assistant",
            AgentMessage.content.in_(FAILURE_NOTICE_TEMPLATES),
        )
    )
    return FAILURE_NOTICE_TEMPLATES[(prior or 0) % len(FAILURE_NOTICE_TEMPLATES)]


def _has_failure_notice_today(
    db: Session,
    agent_session_id: str,
    image_session_id: str,
    day_start: datetime,
) -> bool:
    """同一天同一会话同一 image_session 只通知一次（按模板内容识别失败通知消息）。"""
    latest = db.scalar(
        select(AgentMessage.id)
        .where(
            AgentMessage.session_id == agent_session_id,
            AgentMessage.role == "assistant",
            AgentMessage.image_session_id == image_session_id,
            AgentMessage.content.in_(FAILURE_NOTICE_TEMPLATES),
            AgentMessage.created_at >= day_start,
        )
        .limit(1)
    )
    return latest is not None


def notify_agent_session_of_failure(
    db: Session,
    image_session_id: str,
    failure_reason: str,
    task_kind: str,
) -> list[str]:
    """生图任务终态失败时，向关联的 agent 会话写入一条人话失败通知。

    - 关联方式：agent_sessions.image_session_id 直接匹配，或 agent_messages.image_session_id 反查。
    - 找不到关联会话时静默跳过；本函数只在极端异常时抛错，由调用方决定是否兜底。
    - failure_reason / task_kind 只进日志，绝不写进用户可见消息。
    """
    if not image_session_id:
        return []
    # 技术细节只留日志（含任务种类与失败原因），用户消息里一个字都不出现
    logger.warning(
        "生图任务终态失败，准备回灌 agent 会话通知: image_session_id=%s task_kind=%s failure_reason=%s",
        image_session_id,
        task_kind,
        failure_reason,
    )
    agent_session_ids = _agent_session_ids_for_image_session(db, image_session_id)
    if not agent_session_ids:
        return []
    now = datetime.now(UTC)
    day_start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    notified: list[str] = []
    for agent_session_id in agent_session_ids:
        if _has_failure_notice_today(db, agent_session_id, image_session_id, day_start):
            continue
        content = _failure_notice_template_for(db, agent_session_id)
        db.add(
            AgentMessage(
                session_id=agent_session_id,
                role="assistant",
                content=content,
                image_session_id=image_session_id,
            )
        )
        db.commit()
        notified.append(agent_session_id)
    return notified
