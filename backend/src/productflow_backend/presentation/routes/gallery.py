from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from productflow_backend.application.gallery import list_gallery_entries, save_generated_asset_to_gallery
from productflow_backend.infrastructure.db.models import UserAccount
from productflow_backend.presentation.deps import get_session, require_admin, require_business_user
from productflow_backend.presentation.schemas.gallery import (
    GalleryEntryListResponse,
    GalleryEntryResponse,
    SaveGalleryEntryRequest,
    serialize_gallery_entry,
)

router = APIRouter(prefix="/api/gallery", tags=["gallery"], dependencies=[Depends(require_admin)])


@router.get("", response_model=GalleryEntryListResponse)
def list_gallery_entries_endpoint(
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> GalleryEntryListResponse:
    owner_id = str(user.id) if user is not None else None
    items = list_gallery_entries(session, owner_id=owner_id)
    return GalleryEntryListResponse(items=[serialize_gallery_entry(item) for item in items])


@router.post("", response_model=GalleryEntryResponse, status_code=status.HTTP_201_CREATED)
def save_gallery_entry_endpoint(
    payload: SaveGalleryEntryRequest,
    response: Response,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> GalleryEntryResponse:
    # 画廊条目归属=提交者：由资产所属会话的 owner 派生，隔离开启时先做归属判定
    owner_id = str(user.id) if user is not None else None
    result = save_generated_asset_to_gallery(
        session,
        image_session_asset_id=payload.image_session_asset_id,
        owner_id=owner_id,
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return serialize_gallery_entry(result.entry)
