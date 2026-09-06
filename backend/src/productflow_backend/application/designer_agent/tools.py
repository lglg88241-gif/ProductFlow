from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from productflow_backend.application.asset_library import (
    analyze_asset,
    get_asset_entry,
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
        if name == "recommend_designs":
            return _run_recommend_designs(db, llm, arguments)
        if name == "write_copy_report":
            return _run_write_copy_report(llm, arguments)
        return {"status": "error", "message": f"未知工具: {name}"}
    except Exception as exc:  # noqa: BLE001 - 面向用户的工具错误必须转译
        return {"status": "error", "message": _friendly_generation_error(exc), "detail": str(exc)[:200]}


def _run_generation(
    db: Session,
    agent_session: AgentSession,
    *,
    prompt: str,
    size: Any,
    base_asset_id: str | None,
) -> dict[str, Any]:
    if not prompt.strip():
        return {"status": "error", "message": "图片描述为空，需要先明确画什么。"}
    resolved_size = normalize_image_generation_size(size or DEFAULT_IMAGE_SIZE)
    image_session_id = _ensure_agent_image_session(db, agent_session)
    detail = submit_image_session_generation_task(
        db,
        image_session_id=image_session_id,
        prompt=prompt,
        size=resolved_size,
        base_asset_id=base_asset_id,
        generation_count=1,
    )
    summary = _generation_summary(detail)
    summary["prompt"] = prompt
    summary["size"] = resolved_size
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


def _run_recommend_designs(db: Session, llm: AgentLLMClient, arguments: dict[str, Any]) -> dict[str, Any]:
    requirement = str(arguments.get("requirement", "")).strip()
    if not requirement:
        return {"status": "error", "message": "推荐需求为空，先弄清楚用户想要什么。"}
    candidates = search_asset_entries(db, requirement, kind=arguments.get("kind"), limit=3)
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
