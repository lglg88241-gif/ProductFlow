"""用量查询接口：设计师 Agent 会话/轮次/token 聚合（供运维与成本观测）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from productflow_backend.application.isolation import owner_filter_expression
from productflow_backend.application.time import now_utc
from productflow_backend.infrastructure.db.models import AgentMessage, AgentSession, UserAccount
from productflow_backend.presentation.deps import get_session, require_admin, require_business_user

router = APIRouter(prefix="/api/metrics", tags=["metrics"], dependencies=[Depends(require_admin)])

# by_day 覆盖的天数（含当天）
_BY_DAY_WINDOW_DAYS = 7


class MetricsDailyUsage(BaseModel):
    date: date
    prompt_tokens: int
    completion_tokens: int
    turns: int


class MetricsSummaryResponse(BaseModel):
    total_sessions: int
    total_turns: int
    total_tokens: int
    by_day: list[MetricsDailyUsage]


def _metrics_summary(db: Session, owner_id: str | None = None) -> MetricsSummaryResponse:
    """SQL 聚合：会话/轮次/token；隔离开启时只统计当前用户（owner_id 非 None）。

    消息按"所属会话归属"过滤（消息本身不重复存归属）：join AgentSession 后套用同一过滤条件。
    """
    sessions_stmt = select(func.count()).select_from(AgentSession)
    sessions_owner = owner_filter_expression(AgentSession.owner_id, owner_id)
    if sessions_owner is not None:
        sessions_stmt = sessions_stmt.where(sessions_owner)
    total_sessions = int(db.scalar(sessions_stmt) or 0)

    messages_base = select(AgentMessage).join(
        AgentSession, AgentMessage.session_id == AgentSession.id
    )
    messages_owner = owner_filter_expression(AgentSession.owner_id, owner_id)
    if messages_owner is not None:
        messages_base = messages_base.where(messages_owner)
    messages_subquery = messages_base.subquery()

    total_turns = int(
        db.scalar(
            select(func.count())
            .select_from(messages_subquery)
            .where(messages_subquery.c.role == "user")
        )
        or 0
    )
    total_tokens = int(
        db.scalar(select(func.coalesce(func.sum(messages_subquery.c.total_tokens), 0))) or 0
    )

    today = now_utc().date()
    window_start = datetime.combine(
        today - timedelta(days=_BY_DAY_WINDOW_DAYS - 1), datetime.min.time(), tzinfo=UTC
    )
    rows = db.execute(
        select(
            func.date(messages_subquery.c.created_at).label("day"),
            func.coalesce(func.sum(messages_subquery.c.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(messages_subquery.c.completion_tokens), 0).label("completion_tokens"),
            func.sum(case((messages_subquery.c.role == "user", 1), else_=0)).label("turns"),
        )
        .select_from(messages_subquery)
        .where(messages_subquery.c.created_at >= window_start)
        .group_by(func.date(messages_subquery.c.created_at))
    ).all()
    usage_by_day: dict[str, tuple[int, int, int]] = {
        str(row.day): (int(row.prompt_tokens), int(row.completion_tokens), int(row.turns)) for row in rows
    }

    by_day: list[MetricsDailyUsage] = []
    for offset in range(_BY_DAY_WINDOW_DAYS - 1, -1, -1):
        day = today - timedelta(days=offset)
        prompt_tokens, completion_tokens, turns = usage_by_day.get(day.isoformat(), (0, 0, 0))
        by_day.append(
            MetricsDailyUsage(
                date=day,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                turns=turns,
            )
        )
    return MetricsSummaryResponse(
        total_sessions=total_sessions,
        total_turns=total_turns,
        total_tokens=total_tokens,
        by_day=by_day,
    )


@router.get("/summary", response_model=MetricsSummaryResponse)
def metrics_summary_endpoint(
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> MetricsSummaryResponse:
    """隔离开启时只返回当前用户的用量；关闭时保持全量口径（运维视角）。"""
    from productflow_backend.application.isolation import current_owner_id

    return _metrics_summary(session, current_owner_id(user))
