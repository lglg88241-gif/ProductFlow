from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
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
from productflow_backend.application.designer_agent.tool_schemas import (
    DEFAULT_GENERATION_COUNT,
    GENERATION_COUNT_MAX,
    GENERATION_COUNT_MIN,
    SUPPORTED_EXPORT_GRIDS,
    TOOL_SCHEMAS,
)
from productflow_backend.application.image_sessions import (
    create_image_session,
    get_image_session_detail,
    submit_image_session_generation_task,
)
from productflow_backend.config import normalize_image_generation_size
from productflow_backend.infrastructure.db.models import AgentSession
from productflow_backend.infrastructure.safe_errors import classify_error, sanitize_error_text

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_SIZE = "1024x1024"


def _normalize_generation_count(value: Any) -> int:
    """把 LLM 传入的 count 收敛到 1~4 的合法区间。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return DEFAULT_GENERATION_COUNT
    return min(max(count, GENERATION_COUNT_MIN), GENERATION_COUNT_MAX)


def tool_schemas() -> list[dict[str, Any]]:
    """工具集的 OpenAI function-calling schema（三段式：做什么/何时用/何时不用）。"""
    return TOOL_SCHEMAS



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


@dataclass(frozen=True)
class ToolContext:
    """一次工具调用的全部输入：DB、会话、LLM 与模型给出的参数。"""

    db: Session
    agent_session: AgentSession
    llm: AgentLLMClient
    arguments: dict[str, Any]


def _run_search_assets(ctx: ToolContext) -> dict[str, Any]:
    entries = search_asset_entries(
        ctx.db,
        str(ctx.arguments.get("query", "")),
        kind=ctx.arguments.get("kind"),
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


def _run_analyze_template(ctx: ToolContext) -> dict[str, Any]:
    entry = get_asset_entry(ctx.db, str(ctx.arguments.get("asset_id", "")))
    analyzed = analyze_asset(ctx.db, entry, ctx.llm)
    return {"status": "completed", **_asset_summary(analyzed)}


def _run_save_asset(ctx: ToolContext) -> dict[str, Any]:
    db = ctx.db
    agent_session = ctx.agent_session
    arguments = ctx.arguments
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
    _auto_tag_entry(db, entry, ctx.llm)
    return {"status": "completed", "message": "已存入素材库。", **_asset_summary(entry)}


# 工具注册表：每个工具在此登记一次「schema（在 tool_schemas.py）+ 执行器 + 执行后阶段」。
# 加新工具 = 加一条 schema + 加一条注册项，无需再改 if 链或状态映射表。
_TOOL_HANDLERS: dict[str, Callable[[ToolContext], dict[str, Any]]] = {
    "generate_image": lambda ctx: _run_generation(
        ctx.db,
        ctx.agent_session,
        prompt=str(ctx.arguments.get("prompt", "")),
        size=ctx.arguments.get("size"),
        base_asset_id=None,
        template_asset_id=str(ctx.arguments.get("template_asset_id", "")) or None,
        generation_count=_normalize_generation_count(ctx.arguments.get("count")),
    ),
    "edit_image": lambda ctx: _run_generation(
        ctx.db,
        ctx.agent_session,
        prompt=str(ctx.arguments.get("prompt", "")),
        size=ctx.arguments.get("size"),
        base_asset_id=str(ctx.arguments.get("asset_id", "")) or None,
    ),
    "write_copy": lambda ctx: _run_write_copy(ctx.llm, ctx.arguments),
    "search_assets": _run_search_assets,
    "analyze_template": _run_analyze_template,
    "save_asset": _run_save_asset,
    "export_moments_grid": lambda ctx: _run_export_grid(ctx.db, ctx.agent_session, ctx.arguments),
    "run_product_pipeline": lambda ctx: _run_product_pipeline(ctx.db, ctx.agent_session, ctx.arguments),
    "check_pipeline_status": lambda ctx: _run_check_pipeline(ctx.db, ctx.arguments),
    "recommend_designs": lambda ctx: _run_recommend_designs(ctx.db, ctx.llm, ctx.arguments),
    "write_copy_report": lambda ctx: _run_write_copy_report(ctx.db, ctx.agent_session, ctx.llm, ctx.arguments),
    "rerender_poster_copy": lambda ctx: _run_rerender_poster_copy(ctx.db, ctx.arguments),
}

# 每个工具执行成功后会话应进入的阶段（唯一真相：prompts.STAGE_BY_TOOL 由此派生）
_TOOL_STAGES: dict[str, str] = {
    "generate_image": "review",
    "edit_image": "review",
    "rerender_poster_copy": "review",
    "save_asset": "review",
    "export_moments_grid": "review",
    "write_copy": "produce",
    "write_copy_report": "produce",
    "run_product_pipeline": "produce",
    "check_pipeline_status": "produce",
    "recommend_designs": "recommend",
    "search_assets": "recommend",
    "analyze_template": "recommend",
}


def tool_stages() -> dict[str, str]:
    """工具名 → 执行后阶段；供 loop 推进会话阶段使用。"""
    return dict(_TOOL_STAGES)


def tool_names() -> tuple[str, ...]:
    """注册表中的全部工具名（schema 与执行器必须一一对应，测试会校验）。"""
    return tuple(_TOOL_HANDLERS)


def execute_tool(
    db: Session,
    agent_session: AgentSession,
    *,
    name: str,
    arguments: dict[str, Any],
    llm: AgentLLMClient,
) -> dict[str, Any]:
    """执行一个工具调用；异常转译为人话 JSON 交回 LLM，绝不向上抛原始错误。"""
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return {"status": "error", "message": f"未知工具: {name}"}
    ctx = ToolContext(db=db, agent_session=agent_session, llm=llm, arguments=arguments)
    try:
        return handler(ctx)
    except Exception as exc:  # noqa: BLE001 - 面向用户的工具错误必须转译
        # 原始异常可能含内部路径/供应商响应片段：绝不进入 LLM 上下文或落库消息，
        # 抹除密钥后的摘要只进日志；LLM/前端只见人话 + 结构化错误分类。
        logger.warning(
            "Agent 工具执行失败: tool=%s error=%s",
            name,
            sanitize_error_text(f"{type(exc).__name__}: {exc}"),
        )
        return {"status": "error", "message": _friendly_generation_error(exc), "error": classify_error(exc)}


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


def _latest_generation_task(detail: Any):
    """会话里最新一次提交的生成任务（relationship 按 created_at 升序）。"""
    tasks = list(getattr(detail, "generation_tasks", []) or [])
    return tasks[-1] if tasks else None


def _generation_candidates(detail: Any) -> tuple[list[dict[str, Any]], bool]:
    """提取本轮（最新一次生成任务）的候选与就绪状态。

    返回 (candidates, pending)：
    - 内联完成（测试/同步路径）：候选按 candidate_index 排序返回；
    - durable 队列异步提交（生产路径）：任务还在排队/执行时返回空候选 + pending=True，
      部分候选就绪时返回已有候选并保留 pending=True。
    """
    latest_task = _latest_generation_task(detail)
    if latest_task is None:
        return [], False
    group_id = latest_task.result_generation_group_id
    group_rounds = [
        round_item
        for round_item in (getattr(detail, "rounds", []) or [])
        if group_id and round_item.generation_group_id == group_id and round_item.generated_asset is not None
    ]
    group_rounds.sort(key=lambda item: (item.candidate_index, item.created_at, item.id))
    candidates = [
        {
            "asset_id": round_item.generated_asset.id,
            "url": f"/api/image-session-assets/{round_item.generated_asset.id}/download",
            "label": f"候选 {round_item.candidate_index}",
        }
        for round_item in group_rounds
    ]
    pending = latest_task.status in {"queued", "running"}
    return candidates, pending


def _run_generation(
    db: Session,
    agent_session: AgentSession,
    *,
    prompt: str,
    size: Any,
    base_asset_id: str | None,
    template_asset_id: str | None = None,
    generation_count: int = 1,
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
        generation_count=generation_count,
    )
    summary = _generation_summary(detail)
    summary["prompt"] = effective_prompt
    summary["size"] = resolved_size
    summary["expected_candidates"] = generation_count
    candidates, pending = _generation_candidates(detail)
    summary["candidates"] = candidates
    if pending:
        summary["pending"] = True
    if candidates:
        summary["primary_url"] = candidates[0]["url"]
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
  "title": "报告标题（如：XX 开业朋友圈文案报告）",
  "content": "markdown 正文，依次包含：## 朋友圈正文（2-4 行，口语化，有行动号召）、\
## 卖点清单（3 条左右）、## 话题标签、## 发布建议（时间/配图张数/互动引导）"
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


def _strip_code_fence(text: str) -> str:
    """剥掉 LLM 输出常用的 ```json ... ``` 代码围栏（无围栏时原样返回）。"""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        inner = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = inner.removeprefix("json").removeprefix("JSON").strip()
    return cleaned


def _sections_to_markdown(sections: Any) -> str:
    """把 sections（数组或对象）拼成 markdown 正文，兼容多种键名。"""
    parts: list[str] = []
    if isinstance(sections, dict):
        for key, value in sections.items():
            body = str(value).strip()
            if body:
                parts.append(f"## {key}\n{body}")
    elif isinstance(sections, list):
        for item in sections:
            if isinstance(item, dict):
                heading = str(item.get("heading") or item.get("title") or "").strip()
                body = str(item.get("body") or item.get("content") or item.get("text") or "").strip()
                parts.append(f"## {heading}\n{body}" if heading else body)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
    return "\n\n".join(part for part in parts if part.strip())


def _legacy_report_fields_to_markdown(report: dict[str, Any]) -> str:
    """兼容旧版字段形状（headline/moments_caption/...），拼成 markdown。"""
    parts: list[str] = []
    mapping = (
        ("moments_caption", "朋友圈正文"),
        ("selling_points", "卖点清单"),
        ("hashtags", "话题标签"),
        ("publishing_tips", "发布建议"),
    )
    for key, heading in mapping:
        value = report.get(key)
        if not value:
            continue
        if isinstance(value, list):
            body = "\n".join(f"- {str(item).strip()}" for item in value if str(item).strip())
        else:
            body = str(value).strip()
        if body:
            parts.append(f"## {heading}\n{body}")
    return "\n\n".join(parts)


def _run_write_copy_report(
    db: Session,
    agent_session: AgentSession,
    llm: AgentLLMClient,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """文案报告：LLM 产出 → 解析（剥代码围栏，兼容 content/sections 两种键）→ 落库供下载。"""
    from productflow_backend.infrastructure.db.models import CopyReport

    brief = str(arguments.get("brief", "")).strip()
    if not brief:
        return {"status": "error", "message": "报告需求为空，先明确产品和活动。"}
    tone = str(arguments.get("tone", "")).strip() or "亲切自然"
    response = llm.chat(
        messages=[{"role": "user", "content": _COPY_REPORT_PROMPT.format(brief=brief, tone=tone)}],
        tools=[],
        intent="copy_report",
    )
    title = ""
    content_md = ""
    if response.content:
        try:
            parsed = json.loads(_strip_code_fence(response.content))
            if isinstance(parsed, dict):
                title = str(parsed.get("title") or parsed.get("headline") or "").strip()
                content_md = str(parsed.get("content") or "").strip()
                if not content_md:
                    content_md = _sections_to_markdown(parsed.get("sections"))
                if not content_md:
                    content_md = _legacy_report_fields_to_markdown(parsed)
        except (TypeError, ValueError):
            title, content_md = "", ""
    if not content_md:
        # 解析失败也照常落一份纯文本报告，保证用户拿得到产物
        content_md = (response.content or "").strip()
    if not title:
        title = brief[:40] or "文案报告"
    report = CopyReport(
        agent_session_id=agent_session.id,
        title=title[:255],
        content_md=content_md,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    return {
        "status": "ok",
        "report_id": report.id,
        "title": title,
        "download_url": f"/api/agent/copy-reports/{report.id}/download",
        "preview": content_md[:300],
    }


_RERENDER_COPY_FIELD_LABELS = {
    "price": "价格",
    "product_name": "商品名",
    "headline": "主标题",
    "selling_points": "卖点",
    "instruction": "行动号召",
}


def _rerender_context_lines(context_text: str | None) -> tuple[str, list[str]]:
    """把结构化文案上下文拆成 (主标题, 卖点列表)，兼容 Summary:/摘要：两种前缀。"""
    lines = [line.strip() for line in (context_text or "").splitlines() if line.strip()]
    if not lines:
        return "", []
    first = lines[0]
    if first.lower().startswith("summary:"):
        headline = first.split(":", 1)[1].strip()
    else:
        headline = first.removeprefix("摘要：").strip()
    return headline, lines[1:4]


def _run_rerender_poster_copy(db: Session, arguments: dict[str, Any]) -> dict[str, Any]:
    """参数槽重渲：商品海报只改文字时，本地 PIL 秒级重渲一张新变体（不耗生图额度）。"""
    from productflow_backend.application.contracts import PosterGenerationInput
    from productflow_backend.application.copy_payloads import copy_payload_context_text, validate_copy_payload
    from productflow_backend.domain.enums import CopyStatus, PosterKind, SourceAssetKind
    from productflow_backend.infrastructure.db.models import CopySet, PosterVariant, Product
    from productflow_backend.infrastructure.poster.renderer import PosterRenderer
    from productflow_backend.infrastructure.storage import LocalStorage

    product_id = str(arguments.get("product_id", "")).strip()
    kind_value = str(arguments.get("poster_kind", "")).strip()
    copy_overrides = arguments.get("copy") if isinstance(arguments.get("copy"), dict) else {}
    if not product_id:
        return {"status": "error", "message": "缺少商品 id。"}
    if kind_value not in {"main_image", "promo_poster"}:
        return {"status": "error", "message": "poster_kind 只支持 main_image 或 promo_poster。"}
    known_overrides = {key: value for key, value in copy_overrides.items() if key in _RERENDER_COPY_FIELD_LABELS}
    if not known_overrides:
        return {
            "status": "error",
            "message": (
                "没有可识别的文字字段：请提供 price（价格）、headline（标题）、"
                "selling_points（卖点）、instruction（行动号召）或 product_name 中至少一项。"
            ),
        }
    product = db.get(Product, product_id)
    if product is None:
        return {"status": "error", "message": "找不到这个商品，请确认商品后再试。"}
    kind = PosterKind.MAIN_IMAGE if kind_value == "main_image" else PosterKind.PROMO_POSTER

    original_asset = next(
        (
            asset
            for asset in sorted(product.source_assets, key=lambda item: (item.created_at, item.id), reverse=True)
            if asset.kind == SourceAssetKind.ORIGINAL_IMAGE
        ),
        None,
    )
    if original_asset is None:
        return {"status": "error", "message": "这个商品没有原始商品图，无法本地重渲，请先上传商品图或改用生图工具。"}

    copy_sets = sorted(product.copy_sets, key=lambda item: (item.created_at, item.id), reverse=True)
    copy_set: CopySet | None = None
    headline, selling_points = "", []
    for candidate in copy_sets:
        if not isinstance(candidate.structured_payload, dict):
            continue
        try:
            context_text = copy_payload_context_text(validate_copy_payload(candidate.structured_payload))
        except ValueError:
            continue
        headline, selling_points = _rerender_context_lines(context_text)
        copy_set = candidate
        break

    changed_fields: list[str] = []
    price = str(product.price) if product.price is not None else None
    product_name = product.name
    instruction: str | None = None
    for key, value in known_overrides.items():
        if key == "price" and str(value).strip():
            price = str(value).strip()
        elif key == "product_name" and str(value).strip():
            product_name = str(value).strip()
        elif key == "headline" and str(value).strip():
            headline = str(value).strip()
        elif key == "selling_points" and isinstance(value, list):
            selling_points = [str(item).strip() for item in value if str(item).strip()][:3]
        elif key == "instruction" and str(value).strip():
            instruction = str(value).strip()
        else:
            continue
        changed_fields.append(key)
    if not changed_fields:
        return {"status": "error", "message": "提供的文字字段都是空的，没有需要修改的内容。"}

    context_lines = [f"摘要：{headline}"] if headline else []
    context_lines.extend(selling_points)
    structured_copy_context = "\n".join(context_lines) or None
    effective_instruction = instruction or headline or product_name
    render_input = PosterGenerationInput(
        copy_prompt_mode="copy" if structured_copy_context else "image_edit",
        product_name=product_name,
        category=product.category,
        price=price,
        source_note=product.source_note,
        instruction=effective_instruction,
        structured_copy_context=structured_copy_context,
        source_image=LocalStorage().resolve(original_asset.storage_path),
    )
    content = PosterRenderer().render(render_input, kind)
    width, height = 1080, (1080 if kind == PosterKind.MAIN_IMAGE else 1440)

    storage = LocalStorage()
    relative_path = storage.save_generated_image(
        product.id,
        f"agent-rerender-{kind.value}",
        content,
        suffix=".png",
    )
    if copy_set is None:
        # 商品还没有任何文案版本：落一个占位文案集承接新变体（复用工作流上下文文案的形态）
        copy_set = CopySet(
            product_id=product.id,
            creative_brief_id=None,
            status=CopyStatus.DRAFT,
            structured_payload=None,
            provider_name="agent_rerender",
            model_name="local_renderer",
            prompt_version="v1",
        )
        db.add(copy_set)
        db.flush()
    variant = PosterVariant(
        product_id=product.id,
        copy_set_id=copy_set.id,
        kind=kind,
        template_name=f"agent-rerender:{'default-main' if kind == PosterKind.MAIN_IMAGE else 'default-promo'}",
        storage_path=relative_path,
        mime_type="image/png",
        width=width,
        height=height,
    )
    db.add(variant)
    db.flush()
    variant_id = variant.id
    db.commit()
    return {
        "status": "ok",
        "poster_id": variant_id,
        "poster_kind": kind.value,
        "download_url": f"/api/posters/{variant.id}/download",
        "preview_url": f"/api/posters/{variant.id}/download?variant=preview",
        "changed_fields": changed_fields,
        "message": "海报文字已更新，秒级重渲完成，可在商品页查看新版本。",
    }
