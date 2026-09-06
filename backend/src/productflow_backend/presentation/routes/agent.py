from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from productflow_backend.application.asset_library import (
    ASSET_KINDS,
    delete_asset_entry,
    get_asset_entry,
    list_asset_entries,
    register_asset_upload,
)
from productflow_backend.application.designer_agent.llm import AgentLLMError
from productflow_backend.application.designer_agent.loop import (
    create_agent_session,
    delete_agent_session,
    get_agent_session,
    list_agent_sessions,
    run_agent_turn,
)
from productflow_backend.domain.errors import BusinessError
from productflow_backend.presentation.deps import get_session, require_admin, require_deletion_enabled
from productflow_backend.presentation.image_variants import serve_image_variant
from productflow_backend.presentation.schemas.agent import (
    AgentMessageResponse,
    AgentSessionCreateRequest,
    AgentSessionDetailResponse,
    AgentSessionListResponse,
    AgentSessionResponse,
    AgentTurnRequest,
    AgentTurnResponse,
)

router = APIRouter(prefix="/api/agent", tags=["designer-agent"], dependencies=[Depends(require_admin)])


def _serialize_message(message) -> AgentMessageResponse:
    return AgentMessageResponse(
        id=message.id,
        role=message.role,
        content=message.content,
        tool_name=message.tool_name,
        image_session_id=message.image_session_id,
        created_at=message.created_at,
    )


def _serialize_session(agent_session) -> AgentSessionResponse:
    return AgentSessionResponse(
        id=agent_session.id,
        title=agent_session.title,
        stage=agent_session.stage,
        image_session_id=agent_session.image_session_id,
        created_at=agent_session.created_at,
        updated_at=agent_session.updated_at,
    )


def _serialize_detail(agent_session) -> AgentSessionDetailResponse:
    base = _serialize_session(agent_session)
    return AgentSessionDetailResponse(
        **base.model_dump(),
        messages=[_serialize_message(message) for message in agent_session.messages],
    )


@router.get("/sessions", response_model=AgentSessionListResponse)
def list_agent_sessions_endpoint(session: Session = Depends(get_session)) -> AgentSessionListResponse:
    return AgentSessionListResponse(items=[_serialize_session(item) for item in list_agent_sessions(session)])


@router.post("/sessions", response_model=AgentSessionResponse, status_code=status.HTTP_201_CREATED)
def create_agent_session_endpoint(
    payload: AgentSessionCreateRequest,
    session: Session = Depends(get_session),
) -> AgentSessionResponse:
    return _serialize_session(create_agent_session(session, title=payload.title))


@router.get("/sessions/{agent_session_id}", response_model=AgentSessionDetailResponse)
def get_agent_session_endpoint(
    agent_session_id: str,
    session: Session = Depends(get_session),
) -> AgentSessionDetailResponse:
    return _serialize_detail(get_agent_session(session, agent_session_id))


@router.delete(
    "/sessions/{agent_session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_deletion_enabled)],
)
def delete_agent_session_endpoint(agent_session_id: str, session: Session = Depends(get_session)) -> None:
    delete_agent_session(session, agent_session_id)


def _serialize_asset(entry) -> dict:
    return {
        "id": entry.id,
        "kind": entry.kind,
        "title": entry.title,
        "source": entry.source,
        "mime_type": entry.mime_type,
        "width": entry.width,
        "height": entry.height,
        "tags": entry.vision_tags_json,
        "template_profile": entry.template_profile_json,
        "download_url": f"/api/agent/assets/{entry.id}/download",
        "preview_url": f"/api/agent/assets/{entry.id}/download?variant=preview",
        "thumbnail_url": f"/api/agent/assets/{entry.id}/download?variant=thumbnail",
        "created_at": entry.created_at.isoformat(),
    }


@router.get("/assets")
def list_agent_assets_endpoint(
    kind: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    if kind and kind not in ASSET_KINDS:
        raise HTTPException(status_code=400, detail=f"素材类型不支持: {kind}")
    return {"items": [_serialize_asset(entry) for entry in list_asset_entries(session, kind=kind)]}


@router.post("/assets", status_code=status.HTTP_201_CREATED)
async def upload_agent_asset_endpoint(
    file: UploadFile = File(...),
    kind: str = Form(default="template"),
    session: Session = Depends(get_session),
) -> dict:
    from productflow_backend.presentation.upload_validation import read_validated_image_upload

    try:
        validated = await read_validated_image_upload(file, fallback_filename="asset.bin")
        entry = register_asset_upload(
            session,
            kind=kind if kind in ASSET_KINDS else "template",
            filename=validated.filename,
            content=validated.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_asset(entry)


@router.get("/assets/{asset_id}/download")
def download_agent_asset_endpoint(
    asset_id: str,
    variant: str = "original",
    session: Session = Depends(get_session),
):
    entry = get_asset_entry(session, asset_id)
    try:
        return serve_image_variant(
            storage_path=entry.storage_path,
            original_filename=f"asset-{entry.id}{entry.mime_type.split('/')[-1]}",
            mime_type=entry.mime_type,
            variant=variant,  # type: ignore[arg-type]
            missing_file_detail="素材文件不存在",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_deletion_enabled)],
)
def delete_agent_asset_endpoint(asset_id: str, session: Session = Depends(get_session)) -> None:
    delete_asset_entry(session, asset_id)


@router.post("/sessions/{agent_session_id}/messages", response_model=AgentTurnResponse)
def send_agent_message_endpoint(
    agent_session_id: str,
    payload: AgentTurnRequest,
    session: Session = Depends(get_session),
) -> AgentTurnResponse:
    try:
        result = run_agent_turn(session, agent_session_id=agent_session_id, user_content=payload.content)
    except BusinessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AgentLLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session.expire_all()
    session_detail = get_agent_session(session, agent_session_id)
    return AgentTurnResponse(
        session=_serialize_detail(session_detail),
        tool_events=result.tool_events,
        pending_generation_tasks=[
            {
                "image_session_id": task.get("image_session_id"),
                "task_id": task.get("task_id", ""),
                "status": task.get("status", "queued"),
            }
            for task in result.pending_generation_tasks
        ],
    )
