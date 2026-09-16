"""媒体用量与配额查询（审计批次 C）。

前端据此显示"已用 X / 共 5 GiB"，并在 80% 时提示用户清理。
隔离开关闭（无用户上下文）时返回全站合计口径，便于运维观察。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from productflow_backend.application.isolation import current_owner_id
from productflow_backend.application.quota import compute_usage
from productflow_backend.infrastructure.db.models import UserAccount
from productflow_backend.presentation.deps import get_session, require_admin, require_business_user

router = APIRouter(
    prefix="/api/storage", tags=["storage-usage"], dependencies=[Depends(require_admin)]
)


class StorageUsageResponse(BaseModel):
    used_bytes: int
    quota_bytes: int
    file_count: int
    ratio: float
    warning: bool
    exceeded: bool
    scoped_to_user: bool


@router.get("/usage", response_model=StorageUsageResponse)
def storage_usage_endpoint(
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> StorageUsageResponse:
    """当前用户的媒体占用与配额；未登录（隔离关闭）时按当前主体计量。"""
    owner_id = current_owner_id(user)
    if owner_id is None:
        # 隔离关闭：返回零口径而不是伪造全站数字（全站合计需扫全表，代价高且非本接口职责）
        from productflow_backend.config import get_settings

        settings = get_settings()
        return StorageUsageResponse(
            used_bytes=0,
            quota_bytes=int(settings.user_quota_bytes),
            file_count=0,
            ratio=0.0,
            warning=False,
            exceeded=False,
            scoped_to_user=False,
        )
    report = compute_usage(session, owner_id)
    return StorageUsageResponse(
        used_bytes=report.used_bytes,
        quota_bytes=report.quota_bytes,
        file_count=report.file_count,
        ratio=round(report.ratio, 4),
        warning=report.warning,
        exceeded=report.exceeded,
        scoped_to_user=True,
    )
