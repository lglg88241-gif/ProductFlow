from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from productflow_backend.domain.enums import ImageSessionAssetKind
from productflow_backend.domain.errors import BusinessValidationError, NotFoundError
from productflow_backend.infrastructure.db.models import (
    ImageGalleryEntry,
    ImageSession,
    ImageSessionAsset,
    ImageSessionRound,
)


@dataclass(frozen=True, slots=True)
class GallerySaveResult:
    entry: ImageGalleryEntry
    created: bool


def _gallery_entry_query():
    return (
        select(ImageGalleryEntry)
        .options(
            selectinload(ImageGalleryEntry.asset).selectinload(ImageSessionAsset.session),
            selectinload(ImageGalleryEntry.round),
        )
        .order_by(desc(ImageGalleryEntry.created_at))
    )


def list_gallery_entries(session: Session, *, owner_id: str | None = None) -> list[ImageGalleryEntry]:
    """画廊浏览：条目归属=资产所属会话的 owner（即提交者）。

    隔离开启时按归属过滤：本人条目 + 遗留（会话 owner 为空）条目全局可见。
    """
    query = _gallery_entry_query()
    if owner_id is not None:
        query = (
            query.join(ImageGalleryEntry.asset)
            .join(ImageSessionAsset.session)
            .where(or_(ImageSession.owner_id == owner_id, ImageSession.owner_id.is_(None)))
        )
    return list(session.scalars(query).all())


def _get_gallery_entry_by_asset_id(session: Session, image_session_asset_id: str) -> ImageGalleryEntry | None:
    return session.scalar(
        _gallery_entry_query().where(ImageGalleryEntry.image_session_asset_id == image_session_asset_id)
    )


def _load_gallery_asset(session: Session, image_session_asset_id: str) -> ImageSessionAsset:
    asset = session.scalar(
        select(ImageSessionAsset)
        .options(selectinload(ImageSessionAsset.session))
        .where(ImageSessionAsset.id == image_session_asset_id)
    )
    if asset is None:
        raise NotFoundError("会话图片不存在")
    return asset


def _enforce_gallery_asset_owner(asset: ImageSessionAsset, owner_id: str | None) -> None:
    """投画廊前归属判定：跨用户资产 → 404；遗留（会话 owner 为空）资产可由任何人投递。"""
    if owner_id is None or asset.session is None:
        return
    if asset.session.owner_id is not None and asset.session.owner_id != owner_id:
        raise NotFoundError("会话图片不存在")


def save_generated_asset_to_gallery(
    session: Session, *, image_session_asset_id: str, owner_id: str | None = None
) -> GallerySaveResult:
    # 归属判定先于"已存在"短路，避免跨用户探测已有条目
    asset = _load_gallery_asset(session, image_session_asset_id)
    _enforce_gallery_asset_owner(asset, owner_id)
    if asset.kind != ImageSessionAssetKind.GENERATED_IMAGE:
        raise BusinessValidationError("只有生成结果可以保存到画廊")

    existing = _get_gallery_entry_by_asset_id(session, image_session_asset_id)
    if existing is not None:
        return GallerySaveResult(entry=existing, created=False)

    round_item = session.scalar(select(ImageSessionRound).where(ImageSessionRound.generated_asset_id == asset.id))
    if round_item is None:
        raise NotFoundError("生成记录不存在")

    entry = ImageGalleryEntry(
        image_session_asset_id=asset.id,
        image_session_round_id=round_item.id,
    )
    session.add(entry)
    try:
        session.flush()
        entry_id = entry.id
        session.commit()
    except IntegrityError:
        session.rollback()
        existing = _get_gallery_entry_by_asset_id(session, image_session_asset_id)
        if existing is not None:
            return GallerySaveResult(entry=existing, created=False)
        raise
    session.expire_all()
    return GallerySaveResult(
        entry=session.scalar(_gallery_entry_query().where(ImageGalleryEntry.id == entry_id)) or entry,
        created=True,
    )
