from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from productflow_backend.application.admission import get_generation_queue_overview
from productflow_backend.presentation.deps import get_session, require_admin, require_business_user
from productflow_backend.presentation.schemas.generation_queue import (
    GenerationQueueOverviewResponse,
    serialize_generation_queue_overview,
)

# 队列概览只暴露全局聚合计数（running/queued/并发上限），不含任何归属行；
# 隔离开启时仍要求业务用户会话（401 门禁），但数据无需按 owner 过滤。
router = APIRouter(
    prefix="/api/generation-queue",
    tags=["generation-queue"],
    dependencies=[Depends(require_admin), Depends(require_business_user)],
)


@router.get("", response_model=GenerationQueueOverviewResponse)
def get_generation_queue_overview_endpoint(session: Session = Depends(get_session)) -> GenerationQueueOverviewResponse:
    return serialize_generation_queue_overview(get_generation_queue_overview(session))
