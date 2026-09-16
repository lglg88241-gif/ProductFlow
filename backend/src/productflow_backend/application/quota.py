"""用户媒体配额（审计批次 C）：计量、告警与准入判定。

计量来源是**磁盘实际占用**而非数据库计数——数据库没有 size 列，而磁盘 stat 是
唯一不会撒谎的口径（也不需要在写入路径上维护易漂移的计数）。

覆盖全部四个媒体载体表（漏一张就会算少）：
  asset_library（有 owner）· source_assets / poster_variants（经 products 继承）
  · image_session_assets（经 image_sessions 继承）

已知限制（诚实记录，不假装做到）：
- 并发请求的额度**预留**未实现：两个请求同时通过准入检查时最多可超出约一张图的额度。
  真正的预留需要媒体对象表 + 事务性占用，属后续工作。
- 计量按请求实时 stat，有 3 秒 TTL 缓存（同一用户的连续上传不会重复全量扫描）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from productflow_backend.domain.errors import BusinessError
from productflow_backend.infrastructure.db.models import (
    AssetLibraryEntry,
    ImageSession,
    ImageSessionAsset,
    PosterVariant,
    Product,
    SourceAsset,
)
from productflow_backend.infrastructure.storage import LocalStorage

# 计量结果的缓存 TTL（秒）：避免同一用户连续上传时反复全量 stat
_USAGE_CACHE_TTL_SECONDS = 3.0
_usage_cache: dict[str, tuple[float, UsageReport]] = {}


@dataclass(frozen=True)
class UsageReport:
    """一个用户的媒体占用快照。"""

    used_bytes: int
    file_count: int
    quota_bytes: int
    warn_ratio: float

    @property
    def ratio(self) -> float:
        if self.quota_bytes <= 0:
            return 0.0
        return self.used_bytes / self.quota_bytes

    @property
    def warning(self) -> bool:
        """是否已达告警线（默认 80%）。"""
        return self.ratio >= self.warn_ratio

    @property
    def exceeded(self) -> bool:
        return self.used_bytes >= self.quota_bytes


def _quota_settings() -> tuple[int, float]:
    from productflow_backend.config import get_settings

    settings = get_settings()
    return int(settings.user_quota_bytes), float(settings.user_quota_warn_ratio)


def _collect_media_paths(db: Session, owner_id: str) -> list[str]:
    """汇集该用户全部媒体文件的存储路径（四张表，含经父级继承归属的）。"""
    paths: list[str] = []

    # 素材库：直接按 owner
    paths += list(
        db.scalars(
            select(AssetLibraryEntry.storage_path).where(AssetLibraryEntry.owner_id == owner_id)
        ).all()
    )
    # 商品源图与海报变体：经 products 继承
    product_ids = list(db.scalars(select(Product.id).where(Product.owner_id == owner_id)).all())
    if product_ids:
        paths += list(
            db.scalars(select(SourceAsset.storage_path).where(SourceAsset.product_id.in_(product_ids))).all()
        )
        paths += list(
            db.scalars(select(PosterVariant.storage_path).where(PosterVariant.product_id.in_(product_ids))).all()
        )
    # 图片会话资产：经 image_sessions 继承
    session_ids = list(db.scalars(select(ImageSession.id).where(ImageSession.owner_id == owner_id)).all())
    if session_ids:
        paths += list(
            db.scalars(
                select(ImageSessionAsset.storage_path).where(ImageSessionAsset.session_id.in_(session_ids))
            ).all()
        )
    return paths


def _size_of(storage: LocalStorage, relative_path: str) -> int:
    """单个媒体文件的实际字节数；文件缺失计 0（不因缺文件让计量失败）。"""
    try:
        return Path(storage.resolve(relative_path)).stat().st_size
    except (OSError, ValueError):
        return 0


def compute_usage(db: Session, owner_id: str, *, use_cache: bool = True) -> UsageReport:
    """计算该用户的媒体占用（默认走 3 秒缓存）。"""
    now = time.monotonic()
    if use_cache:
        cached = _usage_cache.get(owner_id)
        if cached is not None and now - cached[0] < _USAGE_CACHE_TTL_SECONDS:
            return cached[1]

    storage = LocalStorage()
    paths = _collect_media_paths(db, owner_id)
    used = sum(_size_of(storage, path) for path in paths)
    quota_bytes, warn_ratio = _quota_settings()
    report = UsageReport(
        used_bytes=used,
        file_count=len(paths),
        quota_bytes=quota_bytes,
        warn_ratio=warn_ratio,
    )
    _usage_cache[owner_id] = (now, report)
    return report


def invalidate_usage_cache(owner_id: str | None = None) -> None:
    """写入媒体后让计量缓存失效（下一次计量重新 stat）。"""
    if owner_id is None:
        _usage_cache.clear()
    else:
        _usage_cache.pop(owner_id, None)


def check_can_add_media(db: Session, owner_id: str | None, incoming_bytes: int) -> UsageReport | None:
    """准入判定：超出配额抛业务错误（人话）；未超返回当前用量快照。

    owner_id 为 None（隔离关闭/遗留模式）时不做任何限制，返回 None——保持现状。
    """
    if owner_id is None:
        return None
    report = compute_usage(db, owner_id)
    if report.used_bytes + max(0, incoming_bytes) > report.quota_bytes:
        remaining_mb = max(0, report.quota_bytes - report.used_bytes) / (1024 * 1024)
        raise BusinessError(
            f"空间不足：你的额度已用 {report.ratio:.0%}，剩余约 {remaining_mb:.0f} MB。"
            "请先删除不再需要的素材或会话，再试一次。"
        )
    return report
