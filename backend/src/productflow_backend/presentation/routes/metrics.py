"""用量查询接口：设计师 Agent 会话/轮次/token 聚合（供运维与成本观测）。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from productflow_backend.application.time import now_utc
from productflow_backend.infrastructure.db.models import AgentMessage, AgentSession
from productflow_backend.presentation.deps import get_session, require_admin

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


def _metrics_summary(db: Session) -> MetricsSummaryResponse:
    """SQL 聚合：会话/轮次/token 总量为全量口径；by_day 为最近 7 天（含当天，UTC 日界）。"""
    total_sessions = int(db.scalar(select(func.count()).select_from(AgentSession)) or 0)
    total_turns = int(
        db.scalar(select(func.count()).select_from(AgentMessage).where(AgentMessage.role == "user")) or 0
    )
    total_tokens = int(
        db.scalar(select(func.coalesce(func.sum(AgentMessage.total_tokens), 0)).select_from(AgentMessage)) or 0
    )

    today = now_utc().date()
    window_start = datetime.combine(
        today - timedelta(days=_BY_DAY_WINDOW_DAYS - 1), datetime.min.time(), tzinfo=UTC
    )
    rows = db.execute(
        select(
            func.date(AgentMessage.created_at).label("day"),
            func.coalesce(func.sum(AgentMessage.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(AgentMessage.completion_tokens), 0).label("completion_tokens"),
            func.sum(case((AgentMessage.role == "user", 1), else_=0)).label("turns"),
        )
        .where(AgentMessage.created_at >= window_start)
        .group_by(func.date(AgentMessage.created_at))
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
def metrics_summary_endpoint(session: Session = Depends(get_session)) -> MetricsSummaryResponse:
    return _metrics_summary(session)
