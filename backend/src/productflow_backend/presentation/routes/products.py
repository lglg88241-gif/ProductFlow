from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from productflow_backend.application.isolation import current_owner_id, ensure_row_readable
from productflow_backend.application.use_cases import (
    add_reference_images,
    confirm_copy_set,
    create_product,
    delete_product,
    delete_reference_image,
    get_product_detail,
    get_product_history,
    list_products,
    update_copy_set,
)
from productflow_backend.domain.enums import ProductWorkflowState
from productflow_backend.infrastructure.db.models import PosterVariant, Product, SourceAsset, UserAccount
from productflow_backend.infrastructure.storage import ImageVariantName
from productflow_backend.presentation.deps import (
    get_session,
    require_admin,
    require_business_user,
    require_deletion_enabled,
)
from productflow_backend.presentation.image_variants import serve_image_variant
from productflow_backend.presentation.schemas.products import (
    CopySetResponse,
    CopySetUpdateRequest,
    ProductDetailResponse,
    ProductHistoryResponse,
    ProductListResponse,
    serialize_copy_set,
    serialize_poster_variant,
    serialize_product_detail,
    serialize_product_summary,
)
from productflow_backend.presentation.upload_validation import (
    read_validated_image_upload,
    validate_reference_image_count,
)

router = APIRouter(prefix="/api", tags=["products"], dependencies=[Depends(require_admin)])


@router.post("/products", response_model=ProductDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_product_endpoint(
    name: str = Form(...),
    image: UploadFile = File(...),
    reference_images: list[UploadFile] | None = File(default=None),
    category: str | None = Form(default=None),
    price: str | None = Form(default=None),
    source_note: str | None = Form(default=None),
    canvas_template_key: str | None = Form(default=None),
    template_language: str | None = Form(default=None),
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductDetailResponse:
    main_image = await read_validated_image_upload(image, fallback_filename="upload.bin")
    reference_payloads: list[tuple[bytes, str, str]] = []
    validate_reference_image_count(len(reference_images or []))
    for reference_image in reference_images or []:
        validated_reference = await read_validated_image_upload(reference_image, fallback_filename="reference.bin")
        reference_payloads.append(
            (
                validated_reference.content,
                validated_reference.filename,
                validated_reference.mime_type,
            )
        )
    product = create_product(
        session,
        name=name,
        category=category,
        price=price,
        source_note=source_note,
        image_bytes=main_image.content,
        filename=main_image.filename,
        content_type=main_image.mime_type,
        reference_image_uploads=reference_payloads,
        canvas_template_key=canvas_template_key,
        template_language=template_language,
        owner_id=current_owner_id(user),
    )
    return serialize_product_detail(product)


@router.get("/products", response_model=ProductListResponse)
def list_products_endpoint(
    status: ProductWorkflowState | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductListResponse:
    items, total = list_products(
        session, status=status, page=page, page_size=page_size, owner_id=current_owner_id(user)
    )
    return ProductListResponse(
        items=[serialize_product_summary(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/products/{product_id}", response_model=ProductDetailResponse)
def get_product_detail_endpoint(
    product_id: str,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductDetailResponse:
    return serialize_product_detail(get_product_detail(session, product_id, current_owner_id(user)))


@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_deletion_enabled)],
)
def delete_product_endpoint(
    product_id: str,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> None:
    delete_product(session, product_id=product_id, owner_id=current_owner_id(user))


@router.post("/products/{product_id}/reference-images", response_model=ProductDetailResponse)
async def upload_reference_images_endpoint(
    product_id: str,
    reference_images: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductDetailResponse:
    reference_payloads: list[tuple[bytes, str, str]] = []
    validate_reference_image_count(len(reference_images))
    for reference_image in reference_images:
        validated_reference = await read_validated_image_upload(reference_image, fallback_filename="reference.bin")
        reference_payloads.append(
            (
                validated_reference.content,
                validated_reference.filename,
                validated_reference.mime_type,
            )
        )
    product = add_reference_images(
        session,
        product_id=product_id,
        reference_image_uploads=reference_payloads,
        owner_id=current_owner_id(user),
    )
    return serialize_product_detail(product)


@router.patch("/copy-sets/{copy_set_id}", response_model=CopySetResponse)
def update_copy_set_endpoint(
    copy_set_id: str,
    payload: CopySetUpdateRequest,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> CopySetResponse:
    copy_set = update_copy_set(
        session,
        copy_set_id=copy_set_id,
        structured_payload=payload.structured_payload,
        owner_id=current_owner_id(user),
    )
    return serialize_copy_set(copy_set)


@router.post("/copy-sets/{copy_set_id}/confirm", response_model=CopySetResponse)
def confirm_copy_set_endpoint(
    copy_set_id: str,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> CopySetResponse:
    copy_set = confirm_copy_set(session, copy_set_id=copy_set_id, owner_id=current_owner_id(user))
    return serialize_copy_set(copy_set)


@router.get("/posters/{poster_id}/download")
def download_poster_endpoint(
    poster_id: str,
    variant: ImageVariantName = Query(default="original"),
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> FileResponse:
    poster = session.get(PosterVariant, poster_id)
    if poster is None:
        raise HTTPException(status_code=404, detail="海报不存在")
    # 归属沿产品继承：跨用户下载与"不存在"同文案
    owner_id = current_owner_id(user)
    if owner_id is not None:
        product = session.get(Product, poster.product_id)
        ensure_row_readable(product, owner_id, message="海报不存在")
    return serve_image_variant(
        storage_path=poster.storage_path,
        original_filename=f"{poster.kind.value}{Path(poster.storage_path).suffix or '.png'}",
        mime_type=poster.mime_type,
        variant=variant,
        missing_file_detail="海报文件不存在",
    )


@router.get("/source-assets/{asset_id}/download")
def download_source_asset_endpoint(
    asset_id: str,
    variant: ImageVariantName = Query(default="original"),
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> FileResponse:
    asset = session.get(SourceAsset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="源图不存在")
    owner_id = current_owner_id(user)
    if owner_id is not None:
        product = session.get(Product, asset.product_id)
        ensure_row_readable(product, owner_id, message="源图不存在")
    return serve_image_variant(
        storage_path=asset.storage_path,
        original_filename=asset.original_filename,
        mime_type=asset.mime_type,
        variant=variant,
        missing_file_detail="源图文件不存在",
    )


@router.delete("/source-assets/{asset_id}", response_model=ProductDetailResponse)
def delete_source_asset_endpoint(
    asset_id: str,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductDetailResponse:
    product = delete_reference_image(session, asset_id=asset_id, owner_id=current_owner_id(user))
    return serialize_product_detail(product)


@router.get("/products/{product_id}/history", response_model=ProductHistoryResponse)
def get_product_history_endpoint(
    product_id: str,
    session: Session = Depends(get_session),
    user: UserAccount | None = Depends(require_business_user),
) -> ProductHistoryResponse:
    history = get_product_history(session, product_id, current_owner_id(user))
    return ProductHistoryResponse(
        copy_sets=[serialize_copy_set(item) for item in history["copy_sets"]],
        poster_variants=[serialize_poster_variant(item) for item in history["poster_variants"]],
    )
