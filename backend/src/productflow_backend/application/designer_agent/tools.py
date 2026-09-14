from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from productflow_backend.application.asset_library import (
    analyze_asset,
    get_asset_entry,
    list_asset_entries,
    register_generated_asset,
    search_asset_entries,
)
from productflow_backend.application.designer_agent.llm import AgentLLMClient, AgentLLMError
from productflow_backend.application.image_sessions import (
    create_image_session,
    get_image_session_detail,
    submit_image_session_generation_task,
)
from productflow_backend.config import normalize_image_generation_size
from productflow_backend.infrastructure.db.models import AgentSession

DEFAULT_IMAGE_SIZE = "1024x1024"


def tool_schemas() -> list[dict[str, Any]]:
    """M1 工具集的 OpenAI function-calling schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "根据图片描述生成一张新图。异步任务，调用后告知用户正在生成。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "具体的图片描述：主体、构图、色调、光线、文字位内容",
                        },
                        "size": {
                            "type": "string",
                            "description": "宽x高像素，如 1024x1024 方图、1080x1440 朋友圈海报；缺省 1024x1024",
                        },
                        "template_asset_id": {
                            "type": "string",
                            "description": (
                                "可选：素材库中的模板 id。用户选中/上传了模板、要求'按这个风格'时必填，"
                                "系统会把该模板的布局/配色/字体气质约束注入生成提示，实现风格复刻。"
                            ),
                        },
                    },
                    "required": ["prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit_image",
                "description": "基于会话中已有的某张图进行修改（换背景/调色/加元素等）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string", "description": "要修改的图片资产 id"},
                        "prompt": {"type": "string", "description": "修改要求的具体描述"},
                        "size": {"type": "string", "description": "可选，输出尺寸 宽x高"},
                    },
                    "required": ["asset_id", "prompt"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_copy",
                "description": "生成营销文案（朋友圈配文、标题、卖点），一次给 2-3 版供用户挑选。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brief": {"type": "string", "description": "文案需求：产品/活动/受众/语气"},
                        "kind": {
                            "type": "string",
                            "enum": ["moments", "headline", "selling_points"],
                            "description": "moments=朋友圈配文 headline=标题 selling_points=卖点清单",
                        },
                        "count": {"type": "integer", "description": "版本数，默认 3"},
                    },
                    "required": ["brief"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_assets",
                "description": "在素材库里按关键词检索模板/参考图/成品（支持风格、色调、场景等标签）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "检索词，如 开业 粉色 战报 高级感"},
                        "kind": {
                            "type": "string",
                            "enum": ["template", "reference", "output", "brand"],
                            "description": "可选，限定素材类型",
                        },
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analyze_template",
                "description": "识别素材库中某张模板图的布局/配色/文案位，生成结构化模板档案。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string", "description": "素材库中的素材 id"},
                    },
                    "required": ["asset_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "save_asset",
                "description": "把会话里生成的某张图存入素材库，方便以后复用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {"type": "string", "description": "图片会话中的资产 id，缺省保存最新产出"},
                        "title": {"type": "string", "description": "可选，素材命名"},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_product_pipeline",
                "description": (
                    "商品批量流水线：用一张商品图自动跑完整工作流（商品理解→文案→生图），"
                    "适合批量出全套素材。异步任务，提交后告知用户到商品页看进度，并可用 "
                    "check_pipeline_status 查询结果。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string", "description": "商品名称"},
                        "library_asset_id": {
                            "type": "string",
                            "description": "素材库中作为商品主图的图片 id（用户上传的商品图）",
                        },
                        "category": {"type": "string", "description": "可选，商品类目"},
                        "price": {"type": "string", "description": "可选，价格"},
                        "canvas_template_key": {
                            "type": "string",
                            "description": "可选，画布模板 key（缺省用电商主图模板）",
                        },
                    },
                    "required": ["product_name", "library_asset_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "check_pipeline_status",
                "description": "查询商品流水线的执行状态与产出（运行状态/海报下载数量/失败原因）。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product_id": {"type": "string", "description": "商品 id"},
                    },
                    "required": ["product_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "export_moments_grid",
                "description": (
                    "把一张成品/素材切成朋友圈分格切片（九宫格等），返回 zip 下载链接。"
                    "用户说'发朋友圈''切九宫格''做多图'时使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "asset_id": {
                            "type": "string",
                            "description": "素材库素材 id；缺省使用本会话最新产出的图",
                        },
                        "grid": {
                            "type": "string",
                            "enum": ["3x3", "2x2", "3x1", "1x3"],
                            "description": "分格方式，默认 3x3 九宫格",
                        },
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recommend_designs",
                "description": "结合用户需求检索素材库，产出 2-3 个带理由的设计方案推荐。用户没头绪时主动使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "requirement": {
                            "type": "string",
                            "description": "用户需求概括：行业/活动/想要的感觉",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["template", "reference", "output", "brand"],
                            "description": "可选，限定检索的素材类型",
                        },
                    },
                    "required": ["requirement"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_copy_report",
                "description": "产出一份完整文案报告：标题、朋友圈正文、卖点清单、话题标签与发布建议。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "brief": {"type": "string", "description": "产品/活动/受众/目标"},
                        "tone": {"type": "string", "description": "可选，语气偏好（亲切/高端/促销感）"},
                    },
                    "required": ["brief"],
                },
            },
        },
    ]


def _ensure_agent_image_session(db: Session, agent_session: AgentSession) -> str:
    """每个 Agent 会话绑定一个图片会话，产出资产全部落在里面。"""
    if agent_session.image_session_id:
        return agent_session.image_session_id
    image_session = create_image_session(db, title=f"设计师·{agent_session.title}")
    agent_session.image_session_id = image_session.id
    db.commit()
    return image_session.id


def _friendly_generation_error(exc: Exception) -> str:
    text = str(exc)
    if "上限" in text or "capacity" in text.lower():
        return "当前生成任务较多，请稍等片刻再试。"
    if "尺寸" in text:
        return "这个尺寸暂不支持，我换个合适的尺寸再来。"
    return "生成请求没有成功，我会换个方式再试一次。"


def _generation_summary(detail: Any) -> dict[str, Any]:
    """把 image session 详情压缩成给 LLM 和前端的最小摘要。"""
    tasks = list(getattr(detail, "generation_tasks", []) or [])
    rounds = list(getattr(detail, "rounds", []) or [])
    latest_rounds = [
        {
            "asset_id": round_item.generated_asset.id,
            "download_url": f"/api/image-session-assets/{round_item.generated_asset.id}/download",
            "preview_url": f"/api/image-session-assets/{round_item.generated_asset.id}/download?variant=preview",
            "size": round_item.size,
        }
        for round_item in rounds[-3:]
        if round_item.generated_asset is not None
    ]
    pending = [
        {"task_id": task.id, "status": task.status}
        for task in tasks
        if task.status in {"queued", "running"}
    ]
    return {
        "image_session_id": detail.id,
        "pending_tasks": pending,
        "completed_assets": latest_rounds,
        "status": "pending" if pending else ("completed" if latest_rounds else "unknown"),
    }


def _asset_summary(entry) -> dict[str, Any]:
    return {
        "asset_id": entry.id,
        "kind": entry.kind,
        "title": entry.title,
        "tags": entry.vision_tags_json,
        "preview_url": f"/api/agent/assets/{entry.id}/download?variant=preview",
        "template_profile": entry.template_profile_json,
    }


def execute_tool(
    db: Session,
    agent_session: AgentSession,
    *,
    name: str,
    arguments: dict[str, Any],
    llm: AgentLLMClient,
) -> dict[str, Any]:
    """执行一个工具调用；异常转译为人话 JSON 交回 LLM，绝不向上抛原始错误。"""
    try:
        if name == "generate_image":
            return _run_generation(
                db, agent_session,
                prompt=str(arguments.get("prompt", "")),
                size=arguments.get("size"),
                base_asset_id=None,
                template_asset_id=str(arguments.get("template_asset_id", "")) or None,
            )
        if name == "edit_image":
            return _run_generation(
                db, agent_session,
                prompt=str(arguments.get("prompt", "")),
                size=arguments.get("size"),
                base_asset_id=str(arguments.get("asset_id", "")) or None,
            )
        if name == "write_copy":
            return _run_write_copy(llm, arguments)
        if name == "search_assets":
            entries = search_asset_entries(
                db,
                str(arguments.get("query", "")),
                kind=arguments.get("kind"),
            )
            return {
                "status": "completed",
                "matches": [_asset_summary(entry) for entry in entries],
                "message": (
                    f"找到 {len(entries)} 个相关素材"
                    if entries
                    else "素材库里没有找到相关的，可以上传一张模板图给我看。"
                ),
            }
        if name == "analyze_template":
            entry = get_asset_entry(db, str(arguments.get("asset_id", "")))
            analyzed = analyze_asset(db, entry, llm)
            return {"status": "completed", **_asset_summary(analyzed)}
        if name == "save_asset":
            image_session_id = agent_session.image_session_id
            if image_session_id is None:
                return {"status": "error", "message": "当前会话还没有生成过图片，没有可保存的内容。"}
            detail = get_image_session_detail(db, image_session_id)
            requested_asset_id = str(arguments.get("asset_id", "")) or None
            candidates = [
                round_item.generated_asset for round_item in detail.rounds if round_item.generated_asset is not None
            ]
            asset = None
            if requested_asset_id:
                asset = next((item for item in candidates if item.id == requested_asset_id), None)
                if asset is None:
                    return {"status": "error", "message": "这张图不在当前会话里，请从会话产出的图片中选。"}
            elif candidates:
                asset = candidates[-1]
            if asset is None:
                return {"status": "error", "message": "当前会话还没有可保存的图片。"}
            from productflow_backend.infrastructure.storage import LocalStorage

            raw = LocalStorage().resolve(asset.storage_path).read_bytes()
            entry = register_generated_asset(
                db,
                content=raw,
                mime_type=asset.mime_type,
                title=str(arguments.get("title") or f"会话产出 {asset.id[:8]}"),
                agent_session_id=agent_session.id,
                image_session_id=image_session_id,
            )
            _auto_tag_entry(db, entry, llm)
            return {"status": "completed", "message": "已存入素材库。", **_asset_summary(entry)}
        if name == "export_moments_grid":
            return _run_export_grid(db, agent_session, arguments)
        if name == "run_product_pipeline":
            return _run_product_pipeline(db, agent_session, arguments)
        if name == "check_pipeline_status":
            return _run_check_pipeline(db, arguments)
        if name == "recommend_designs":
            return _run_recommend_designs(db, llm, arguments)
        if name == "write_copy_report":
            return _run_write_copy_report(llm, arguments)
        return {"status": "error", "message": f"未知工具: {name}"}
    except Exception as exc:  # noqa: BLE001 - 面向用户的工具错误必须转译
        return {"status": "error", "message": _friendly_generation_error(exc), "detail": str(exc)[:200]}


def _template_style_block(entry: Any) -> str:
    """把模板档案转成生成提示的风格约束段（P2-8：识别即可复刻）。"""
    profile = entry.template_profile_json or {}
    parts: list[str] = []
    if profile.get("layout"):
        parts.append(f"布局遵循：{profile['layout']}")
    palette = profile.get("palette")
    if palette:
        joined = " / ".join(str(item) for item in palette) if isinstance(palette, list) else str(palette)
        parts.append(f"配色使用：{joined}")
    if profile.get("typography"):
        parts.append(f"字体气质：{profile['typography']}")
    if profile.get("copy_slots"):
        parts.append(f"文案位安排：{profile['copy_slots']}")
    if profile.get("mood"):
        parts.append(f"整体气质：{profile['mood']}")
    if not parts:
        return f"参考模板「{entry.title}」的整体风格"
    return "严格按照以下模板风格出图（复用其版式语言，不要照抄其中文字与品牌标识）：" + "；".join(parts)


def _run_generation(
    db: Session,
    agent_session: AgentSession,
    *,
    prompt: str,
    size: Any,
    base_asset_id: str | None,
    template_asset_id: str | None = None,
) -> dict[str, Any]:
    if not prompt.strip():
        return {"status": "error", "message": "图片描述为空，需要先明确画什么。"}
    resolved_size = normalize_image_generation_size(size or DEFAULT_IMAGE_SIZE)
    effective_prompt = prompt
    template_title: str | None = None
    if template_asset_id:
        try:
            template_entry = get_asset_entry(db, template_asset_id)
        except Exception:  # noqa: BLE001 - 模板不存在时退回普通生成
            return {"status": "error", "message": "这个模板不在素材库里，请重新选择或先上传。"}
        template_title = template_entry.title
        effective_prompt = prompt + "\n\n" + _template_style_block(template_entry)
    image_session_id = _ensure_agent_image_session(db, agent_session)
    detail = submit_image_session_generation_task(
        db,
        image_session_id=image_session_id,
        prompt=effective_prompt,
        size=resolved_size,
        base_asset_id=base_asset_id,
        generation_count=1,
    )
    summary = _generation_summary(detail)
    summary["prompt"] = effective_prompt
    summary["size"] = resolved_size
    if template_title:
        summary["template_title"] = template_title
    return summary


def _run_write_copy(llm: AgentLLMClient, arguments: dict[str, Any]) -> dict[str, Any]:
    brief = str(arguments.get("brief", "")).strip()
    if not brief:
        return {"status": "error", "message": "文案需求为空，需要先明确产品和活动。"}
    kind = str(arguments.get("kind", "moments"))
    count = min(max(int(arguments.get("count", 3) or 3), 1), 5)
    instruction = (
        f"你是资深营销文案师。请根据以下需求生成 {count} 版{kind}文案，"
        '只返回 JSON 数组，每项形如 {"title": "短标题", "content": "正文", "hashtags": ["标签"]}, '
        "不要包含其他文字或代码块标记。\n需求：" + brief
    )
    response = llm.chat(messages=[{"role": "user", "content": instruction}], tools=[])
    copies: list[dict[str, Any]] = []
    if response.content:
        try:
            parsed = json.loads(response.content)
            if isinstance(parsed, list):
                copies = [item for item in parsed if isinstance(item, dict)]
        except (TypeError, ValueError):
            copies = []
    if not copies and response.content:
        copies = [{"title": "", "content": response.content.strip(), "hashtags": []}]
    return {"status": "completed", "kind": kind, "copies": copies}

_RECOMMEND_RATIONALE_PROMPT = """你是资深设计师。针对用户需求，为每个候选模板写一句"为什么适合"的推荐理由。
只返回 JSON 数组（与候选顺序一致），每项形如 {{"asset_id": "...", "why": "一句话理由（结合模板风格与用户需求）"}}。
用户需求：{requirement}
候选模板（含档案）：
{candidates}
"""

_COPY_REPORT_PROMPT = """你是资深营销文案师。根据需求输出一份完整的文案报告，只返回 JSON 对象：
{{
  "headline": "主标题",
  "moments_caption": "朋友圈正文（2-4 行，口语化，有行动号召）",
  "selling_points": ["卖点1", "卖点2", "卖点3"],
  "hashtags": ["话题标签"],
  "publishing_tips": "发布建议（时间/配图张数/互动引导）"
}}
不要包含其他文字或代码块标记。
需求：{brief}
语气：{tone}
"""


def _auto_tag_entry(db: Session, entry, llm: AgentLLMClient) -> None:
    """成品入库时顺手做视觉打标（失败静默，不影响保存结果）。"""
    try:
        analyze_asset(db, entry, llm)
        entry.template_profile_json = None  # 成品不是模板，只保留标签
        db.commit()
        db.refresh(entry)
    except Exception:  # noqa: BLE001 - 打标是增强能力，任何失败都不阻塞保存
        db.rollback()


def _run_product_pipeline(db: Session, agent_session: AgentSession, arguments: dict[str, Any]) -> dict[str, Any]:
    """商品批量流水线：复用冻结的商品工作流（agent 收编为入口，不改动其内部）。"""
    from productflow_backend.application.product_workflow.execution import submit_product_workflow_run
    from productflow_backend.application.use_cases import create_product
    from productflow_backend.infrastructure.storage import LocalStorage

    name = str(arguments.get("product_name", "")).strip()
    library_asset_id = str(arguments.get("library_asset_id", "")).strip()
    if not name:
        return {"status": "error", "message": "商品名不能为空。"}
    if not library_asset_id:
        return {"status": "error", "message": "需要商品主图：请先上传商品图，或告诉我素材库里的图片。"}
    try:
        entry = get_asset_entry(db, library_asset_id)
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "素材库里找不到这张商品图，请确认后再试。"}

    raw = LocalStorage().resolve(entry.storage_path).read_bytes()
    product = create_product(
        db,
        name=name,
        category=_optional_str_arg(arguments, "category"),
        price=_optional_str_arg(arguments, "price"),
        source_note=None,
        image_bytes=raw,
        filename=f"pipeline-{library_asset_id[:8]}.png",
        content_type=entry.mime_type,
        canvas_template_key=str(arguments.get("canvas_template_key") or "ecommerce-main-image-v1"),
    )
    workflow = submit_product_workflow_run(db, product_id=product.id)
    latest_run = workflow.runs[0] if workflow.runs else None
    return {
        "status": "submitted",
        "product_id": product.id,
        "product_name": product.name,
        "run_id": latest_run.id if latest_run else None,
        "run_status": latest_run.status if latest_run else "queued",
        "message": (
            "商品流水线已启动（商品理解→文案→生图）。完成后用户可在商品页查看，"
            "也可以稍后用 check_pipeline_status 查询结果。"
        ),
    }


def _run_check_pipeline(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    from productflow_backend.application.product_workflows import get_product_workflow_status
    from productflow_backend.application.use_cases import get_product_detail

    product_id = str(arguments.get("product_id", "")).strip()
    if not product_id:
        return {"status": "error", "message": "缺少商品 id。"}
    try:
        product = get_product_detail(db, product_id)
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "找不到这个商品。"}

    snapshot = get_product_workflow_status(db, product_id)
    run = snapshot.runs[0] if snapshot.runs else None
    run_status = run.status if run is not None else "未运行"
    posters = [
        {
            "kind": str(variant.kind),
            "download_url": f"/api/posters/{variant.id}/download",
            "preview_url": f"/api/posters/{variant.id}/download?variant=preview",
            "size": f"{variant.width}x{variant.height}",
        }
        for variant in product.poster_variants
    ]
    result: dict[str, Any] = {
        "status": "completed",
        "product_id": product.id,
        "product_name": product.name,
        "run_status": str(run_status),
        "poster_count": len(posters),
        "posters": posters[:4],
    }
    if run is not None and run.failure_reason:
        result["failure_reason"] = str(run.failure_reason)[:200]
    if product.copy_sets:
        latest_copy = product.copy_sets[-1]
        result["copy_confirmed"] = bool(getattr(latest_copy, "confirmed_at", None))
    return result


def _optional_str_arg(arguments: dict[str, Any], key: str) -> str | None:
    value = str(arguments.get(key, "") or "").strip()
    return value or None


SUPPORTED_EXPORT_GRIDS = ("3x3", "2x2", "3x1", "1x3")


def _run_export_grid(db: Session, agent_session: AgentSession, arguments: dict[str, Any]) -> dict[str, Any]:
    """分格导出：素材 id 优先，缺省取本会话最新产出的图。"""
    from productflow_backend.application.asset_library import list_asset_entries

    grid = str(arguments.get("grid") or "3x3")
    if grid not in SUPPORTED_EXPORT_GRIDS:
        grid = "3x3"
    requested = str(arguments.get("asset_id") or "").strip()

    asset_id = requested or None
    if asset_id is None:
        # 本会话最新的一件成品（素材库 source=generated 且同会话）
        candidates = [
            entry
            for entry in list_asset_entries(db, kind="output")
            if entry.agent_session_id == agent_session.id
        ]
        if not candidates:
            return {"status": "error", "message": "还没有可导出的成品图，先做一张吧。"}
        asset_id = candidates[0].id

    try:
        entry = get_asset_entry(db, asset_id)
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "找不到这张图，请从素材库里选一张。"}

    download_url = f"/api/agent/assets/{asset_id}/grid-export?grid={grid}"
    grid_labels = {"3x3": "九宫格", "2x2": "四宫格", "3x1": "三横连", "1x3": "三竖连"}
    return {
        "status": "completed",
        "asset_id": asset_id,
        "title": entry.title,
        "grid": grid,
        "grid_label": grid_labels[grid],
        "download_url": download_url,
        "message": f"已生成{grid_labels[grid]}切片，点下载即可按顺序发朋友圈。",
    }


def _run_recommend_designs(db: Session, llm: AgentLLMClient, arguments: dict[str, Any]) -> dict[str, Any]:
    requirement = str(arguments.get("requirement", "")).strip()
    if not requirement:
        return {"status": "error", "message": "推荐需求为空，先弄清楚用户想要什么。"}
    candidates = search_asset_entries(db, requirement, kind=arguments.get("kind"), limit=3)
    if not candidates:
        # 检索无命中：回落为素材库现有样板，避免推荐空手而归
        template_kind = arguments.get("kind") or "template"
        candidates = [
            entry for entry in list_asset_entries(db, kind=template_kind) if entry.kind == "template"
        ][:3]
    if not candidates:
        return {
            "status": "completed",
            "recommendations": [],
            "message": "素材库里还没有匹配的样板，可以上传一张模板图，或者我直接按需求新画。",
        }
    rationale_map: dict[str, str] = {}
    try:
        candidates_text = json.dumps(
            [
                {"asset_id": entry.id, "title": entry.title, "profile": entry.template_profile_json}
                for entry in candidates
            ],
            ensure_ascii=False,
            indent=1,
        )
        prompt_text = _RECOMMEND_RATIONALE_PROMPT.format(requirement=requirement, candidates=candidates_text)
        response = llm.chat(
            messages=[{"role": "user", "content": prompt_text}],
            tools=[],
            intent="recommend_rationales",
        )
        if response.content:
            parsed = json.loads(response.content.strip().removeprefix("```json").strip("` \n"))
            if isinstance(parsed, list):
                rationale_map = {
                    str(item.get("asset_id")): str(item.get("why", ""))
                    for item in parsed
                    if isinstance(item, dict) and item.get("asset_id")
                }
    except (AgentLLMError, ValueError, TypeError):
        rationale_map = {}
    recommendations = []
    for entry in candidates:
        summary = _asset_summary(entry)
        recommendations.append(
            {
                **summary,
                "why": rationale_map.get(entry.id, "风格与你的需求接近。"),
            }
        )
    return {
        "status": "completed",
        "requirement": requirement,
        "recommendations": recommendations,
        "message": f"为你挑了 {len(recommendations)} 个方案，选中后我可以直接按它出图。",
    }


def _run_write_copy_report(llm: AgentLLMClient, arguments: dict[str, Any]) -> dict[str, Any]:
    brief = str(arguments.get("brief", "")).strip()
    if not brief:
        return {"status": "error", "message": "报告需求为空，先明确产品和活动。"}
    tone = str(arguments.get("tone", "")).strip() or "亲切自然"
    response = llm.chat(
        messages=[{"role": "user", "content": _COPY_REPORT_PROMPT.format(brief=brief, tone=tone)}],
        tools=[],
        intent="copy_report",
    )
    report: dict[str, Any] = {}
    if response.content:
        try:
            cleaned = response.content.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1].removeprefix("json").strip()
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                report = parsed
        except (TypeError, ValueError):
            report = {}
    if not report:
        report = {"moments_caption": (response.content or "").strip()}
    return {"status": "completed", "report": report}
