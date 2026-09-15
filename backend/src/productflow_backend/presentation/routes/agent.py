from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Generator
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

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
    run_agent_turn_events,
)
from productflow_backend.domain.errors import BusinessError
from productflow_backend.infrastructure.db.models import CopyReport
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

logger = logging.getLogger(__name__)

# 路由层兜底的 LLM 故障文案：给用户的是人话，原始异常只进日志
_LLM_FAILURE_USER_MESSAGE = "设计师模型暂时没有响应，请稍等片刻再试一次。"

router = APIRouter(prefix="/api/agent", tags=["designer-agent"], dependencies=[Depends(require_admin)])

# SSE 心跳间隔（秒）：空闲超过该时长输出 ": ping" 注释帧，防止 nginx 默认 60s 空闲断开
DEFAULT_SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0


def _heartbeat_frames(
    inner: Generator[str, None, None],
    *,
    heartbeat_interval: float = DEFAULT_SSE_HEARTBEAT_INTERVAL_SECONDS,
) -> Generator[str, None, None]:
    """给 SSE 帧生成器加心跳保活。

    原事件生成器阻塞在 LLM/工具执行时无法自己发心跳。这里把帧生成放到后台线程消费，
    推入队列；响应生成器带超时取帧，空闲超过 `heartbeat_interval` 秒则输出 SSE 注释帧
    ": ping"（注释帧不是事件，客户端解析需跳过）。

    客户端断开（GeneratorExit）时置停止标志并 join 后台线程：线程里的 turn 会继续执行
    并落库完成，避免留下半截历史（孤儿 tool_calls）。
    """
    frame_queue: queue.Queue = queue.Queue()
    sentinel = object()
    client_gone = threading.Event()

    def _pump() -> None:
        try:
            for frame in inner:
                if client_gone.is_set():
                    # 客户端已断开：不再排队剩余帧，但继续消费以让 turn 落库完成
                    continue
                frame_queue.put(frame)
        except Exception:
            logger.exception("SSE 事件生成器异常终止")
        finally:
            frame_queue.put(sentinel)

    pump_thread = threading.Thread(target=_pump, name="sse-event-pump", daemon=True)
    pump_thread.start()
    try:
        while True:
            try:
                frame = frame_queue.get(timeout=heartbeat_interval)
            except queue.Empty:
                yield ": ping\n\n"
                continue
            if frame is sentinel:
                break
            yield frame
    except GeneratorExit:
        client_gone.set()
        # 等待后台线程把本轮 turn 执行完（继续落库），随后按协议重新抛出
        pump_thread.join()
        raise


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


@router.get("/assets/{asset_id}/grid-export")
async def export_asset_grid_endpoint(
    asset_id: str,
    grid: str = "3x3",
    fmt: str = "png",
    session: Session = Depends(get_session),
):
    """把素材切成朋友圈分格切片（zip 下载）。

    PIL 切图（optimize 压缩）是长 CPU 任务：async 端点显式经 run_in_threadpool
    offload 到工作线程，事件循环在切片期间保持响应；响应行为与同步版完全一致。
    """
    from fastapi import Response

    from productflow_backend.application.grid_export import slice_into_grid
    from productflow_backend.infrastructure.storage import LocalStorage

    entry = get_asset_entry(session, asset_id)
    storage = LocalStorage()
    try:
        raw = await run_in_threadpool(lambda: storage.resolve(entry.storage_path).read_bytes())
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="素材文件不存在") from exc
    result = await run_in_threadpool(slice_into_grid, raw, grid=grid, fmt=fmt)
    filename = f"asset-{asset_id[:8]}-{result.grid}.zip"
    return Response(
        content=result.archive,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Grid-Tiles": str(result.tile_count),
        },
    )


@router.delete(
    "/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_deletion_enabled)],
)
def delete_agent_asset_endpoint(asset_id: str, session: Session = Depends(get_session)) -> None:
    delete_asset_entry(session, asset_id)


def _copy_report_content_disposition(filename: str) -> str:
    """中文文件名按 RFC 5987 编码（与 FileResponse 行为一致），ASCII 名直接内联。"""
    quoted = quote(filename)
    if quoted == filename:
        return f'attachment; filename="{filename}"'
    return f"attachment; filename*=utf-8''{quoted}"


def _copy_report_filename(report: CopyReport) -> str:
    """用报告标题命名下载文件；标题缺省或含非法字符时回退 report-{id}.md。"""
    title = (report.title or "").strip()
    sanitized = "".join(
        char for char in title if char not in '\\/:*?"<>|' and char.isprintable()
    ).strip(". ")
    stem = sanitized or f"report-{report.id}"
    return f"{stem}.md"


@router.get("/copy-reports")
def list_agent_copy_reports_endpoint(
    session_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict:
    """按会话过滤列出文案报告（可选 session_id）。"""
    query = select(CopyReport).order_by(CopyReport.created_at.desc(), CopyReport.id)
    if session_id:
        query = query.where(CopyReport.agent_session_id == session_id)
    reports = list(session.scalars(query).all())
    return {
        "items": [
            {
                "id": report.id,
                "agent_session_id": report.agent_session_id,
                "title": report.title,
                "download_url": f"/api/agent/copy-reports/{report.id}/download",
                "created_at": report.created_at.isoformat(),
            }
            for report in reports
        ]
    }


@router.get("/copy-reports/{report_id}/download")
def download_agent_copy_report_endpoint(
    report_id: str,
    session: Session = Depends(get_session),
) -> Response:
    """下载文案报告 markdown 附件。"""
    report = session.get(CopyReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="文案报告不存在")
    return Response(
        content=report.content_md or "",
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": _copy_report_content_disposition(_copy_report_filename(report)),
        },
    )


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
        logger.warning("设计师模型调用失败（非流式端点兜底）: %s", exc)
        raise HTTPException(status_code=503, detail=_LLM_FAILURE_USER_MESSAGE) from exc
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


@router.post(
    "/sessions/{agent_session_id}/messages/stream",
    responses={200: {"content": {"text/event-stream": {}}}},
)
def send_agent_message_stream_endpoint(
    agent_session_id: str,
    payload: AgentTurnRequest,
    session: Session = Depends(get_session),
) -> Generator[str, None, None]:
    """SSE 流式对话：stage / message / tool_start / tool_result / error / done 帧。"""

    import json as _json

    def _frame(event: str, data: dict) -> str:
        payload = _json.dumps(data, ensure_ascii=False)
        return "event: " + event + "\ndata: " + payload + "\n\n"

    from fastapi.responses import StreamingResponse

    def _generate() -> Generator[str, None, None]:
        try:
            events = run_agent_turn_events(session, agent_session_id=agent_session_id, user_content=payload.content)
            for event in events:
                yield _frame(event["event"], event["data"])
        except (BusinessError, AgentLLMError, ValueError) as exc:
            if isinstance(exc, AgentLLMError):
                logger.warning("设计师模型调用失败（流式端点兜底）: %s", exc)
                user_message = _LLM_FAILURE_USER_MESSAGE
            else:
                user_message = str(exc)
            yield _frame("error", {"message": user_message})
            yield _frame("done", {"session_id": agent_session_id, "stage": "", "image_session_id": None,
                                  "tool_events": [], "pending_generation_tasks": []})

    return StreamingResponse(
        _heartbeat_frames(_generate(), heartbeat_interval=DEFAULT_SSE_HEARTBEAT_INTERVAL_SECONDS),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

