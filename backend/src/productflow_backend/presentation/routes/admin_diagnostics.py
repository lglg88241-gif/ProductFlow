"""管理员诊断端点：承接自 healthz 的部署细节 + 运行环境连通性探测。

healthz 已收敛为最小存活探针（不暴露任何可被未鉴权方侦察的部署细节），
门禁状态与供应商摘要等诊断信息统一收敛到这里，仅管理员可见。
每个子项探测独立容错：单项失败只影响该子项的取值（false / None），
绝不让整个端点 500。
"""

from __future__ import annotations

import logging

import redis as redis_lib
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from productflow_backend import __version__
from productflow_backend.config import get_settings
from productflow_backend.infrastructure.provider_config import (
    resolve_agent_provider_config,
    resolve_image_provider_config,
)
from productflow_backend.presentation.deps import get_session, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-diagnostics"], dependencies=[Depends(require_admin)])

_REDIS_PROBE_TIMEOUT_SECONDS = 2.0


@router.get("/diagnostics")
def diagnostics(session: Session = Depends(get_session)) -> dict[str, object]:
    """运行环境诊断摘要（管理员专用）。"""
    return {
        "app_version": __version__,
        "providers": _provider_status_summary(),
        "db_reachable": _db_reachable(session),
        "redis_reachable": _redis_reachable(),
        "alembic_version": _alembic_version(session),
    }


def _provider_status_summary() -> dict[str, object]:
    """当前生效的供应商摘要（不含任何密钥，也不暴露 host/base_url 等部署拓扑信息）。"""
    summary: dict[str, object] = {}
    try:
        agent = resolve_agent_provider_config()
        summary["agent"] = {
            "kind": agent.provider_kind,
            "model": agent.model,
            "has_key": bool(agent.api_key),
            "has_fallback": bool(agent.fallback_api_key and agent.fallback_model),
        }
    except Exception as exc:  # noqa: BLE001 - 诊断绝不因供应商配置问题失败
        summary["agent"] = {"error": type(exc).__name__}
    try:
        image = resolve_image_provider_config()
        summary["image"] = {
            "kind": image.provider_kind,
            "model": image.model,
            "has_key": bool(image.api_key),
        }
    except Exception as exc:  # noqa: BLE001
        summary["image"] = {"error": type(exc).__name__}
    return summary


def _db_reachable(session: Session) -> bool:
    try:
        session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 数据库不可达只降级该子项，不让端点 500
        logger.warning("诊断探测：数据库不可达", exc_info=True)
        return False
    return True


def _redis_reachable() -> bool:
    settings = get_settings()
    try:
        client = redis_lib.Redis.from_url(
            settings.redis_url,
            socket_connect_timeout=_REDIS_PROBE_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_PROBE_TIMEOUT_SECONDS,
        )
        return bool(client.ping())
    except Exception:  # noqa: BLE001 - Redis 缺失/超时只降级该子项
        logger.warning("诊断探测：Redis 不可达（%s）", settings.redis_url, exc_info=True)
        return False


def _alembic_version(session: Session) -> str | None:
    """alembic_version 表中的当前迁移版本；表不存在或查询失败时返回 None。"""
    try:
        row = session.execute(text("SELECT version_num FROM alembic_version")).scalars().first()
    except Exception:  # noqa: BLE001 - 表不存在/DB 不可达时返回 None，不影响端点
        # 典型场景：测试库用 create_all 建表、或迁移尚未执行。
        return None
    return str(row) if row is not None else None
