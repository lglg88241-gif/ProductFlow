from __future__ import annotations

import json
from base64 import b64encode
from io import BytesIO
from pathlib import Path

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from productflow_backend.application.designer_agent.llm import AgentLLMClient, AgentLLMError
from productflow_backend.domain.errors import BusinessError, NotFoundError
from productflow_backend.infrastructure.db.models import AssetLibraryEntry
from productflow_backend.infrastructure.db.session import get_session_factory
from productflow_backend.infrastructure.storage import LocalStorage

ASSET_KINDS = ("template", "reference", "output", "brand")
ASSET_SOURCES = ("upload", "generated", "builtin")
_BUILTIN_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "assets" / "moments_templates"

TEMPLATE_ANALYSIS_PROMPT = """你是资深平面设计师。分析这张模板图的视觉结构，只返回 JSON 对象（不要其他文字）：
{
  "summary": "一句话概括风格与适用场景",
  "layout": "布局结构描述（如 上图下文/左文右图/大字报）",
  "palette": ["主色调描述", "辅色描述"],
  "typography": "字体气质（如 黑体冲击力强/手写温暖）",
  "copy_slots": "文案位数量与位置",
  "mood": "情绪关键词",
  "suitable_categories": ["适用品类或场景", "..."],
  "tags": ["5-8 个检索用标签，中文"]
}"""


def register_asset_upload(
    db: Session,
    *,
    kind: str,
    filename: str,
    content: bytes,
    agent_session_id: str | None = None,
    image_session_id: str | None = None,
    title: str | None = None,
) -> AssetLibraryEntry:
    """校验并把上传图存入素材库；视觉标注由 analyze_asset 补充。"""
    if kind not in ASSET_KINDS:
        raise BusinessError(f"素材类型不支持: {kind}")
    if not content:
        raise BusinessError("素材内容不能为空")
    from productflow_backend.config import get_runtime_settings

    settings = get_runtime_settings()
    if len(content) > settings.upload_max_image_bytes:
        raise BusinessError("素材超过大小限制")
    try:
        with Image.open(BytesIO(content)) as image:
            width, height = image.size
            mime_map = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
            mime_type = mime_map.get(image.format or "", "image/png")
    except OSError as exc:
        raise BusinessError("素材不是可解码图片") from exc

    storage = LocalStorage()
    storage_path = storage.save_library_asset(kind, filename, content)
    entry = AssetLibraryEntry(
        kind=kind,
        title=(title or Path(filename).stem)[:200],
        source="upload",
        storage_path=storage_path,
        mime_type=mime_type,
        width=width,
        height=height,
        agent_session_id=agent_session_id,
        image_session_id=image_session_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def register_generated_asset(
    db: Session,
    *,
    content: bytes,
    mime_type: str,
    title: str,
    agent_session_id: str | None = None,
    image_session_id: str | None = None,
) -> AssetLibraryEntry:
    storage = LocalStorage()
    suffix = ".png" if mime_type == "image/png" else ".jpg"
    storage_path = storage.save_library_asset("output", f"generated{suffix}", content)
    entry = AssetLibraryEntry(
        kind="output",
        title=title[:200],
        source="generated",
        storage_path=storage_path,
        mime_type=mime_type,
        agent_session_id=agent_session_id,
        image_session_id=image_session_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def analyze_asset(db: Session, entry: AssetLibraryEntry, llm: AgentLLMClient) -> AssetLibraryEntry:
    """用视觉模型生成结构化模板档案与检索标签；失败保留原样由调用方提示。"""
    raw = LocalStorage().resolve(entry.storage_path).read_bytes()
    data_url = f"data:{entry.mime_type};base64,{b64encode(raw).decode('utf-8')}"
    response = llm.chat(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": TEMPLATE_ANALYSIS_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
        tools=[],
    )
    if not response.content:
        raise AgentLLMError("视觉模型没有返回分析结果")
    cleaned = response.content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1].removeprefix("json").strip()
    profile = json.loads(cleaned)
    if not isinstance(profile, dict):
        raise AgentLLMError("视觉模型返回的模板档案格式不正确")
    tags = [str(tag) for tag in profile.get("tags", []) if str(tag).strip()]
    entry.template_profile_json = profile
    entry.vision_tags_json = tags
    if profile.get("summary"):
        entry.title = str(profile["summary"])[:200]
    db.commit()
    db.refresh(entry)
    return entry


def list_asset_entries(db: Session, *, kind: str | None = None) -> list[AssetLibraryEntry]:
    statement = select(AssetLibraryEntry).order_by(AssetLibraryEntry.created_at.desc(), AssetLibraryEntry.id)
    if kind:
        statement = statement.where(AssetLibraryEntry.kind == kind)
    return list(db.scalars(statement).all())


def get_asset_entry(db: Session, asset_id: str) -> AssetLibraryEntry:
    entry = db.get(AssetLibraryEntry, asset_id)
    if entry is None:
        raise NotFoundError("素材不存在")
    return entry


def search_asset_entries(
    db: Session, query: str, *, kind: str | None = None, limit: int = 5
) -> list[AssetLibraryEntry]:
    """轻量语义检索：对标题/标签/模板档案做词项匹配打分（M3 再升级向量检索）。"""
    tokens = [token for token in query.replace("，", " ").replace(",", " ").split() if token]
    scored: list[tuple[int, AssetLibraryEntry]] = []
    for entry in list_asset_entries(db, kind=kind):
        profile_text = json.dumps(entry.template_profile_json or {}, ensure_ascii=False)
        haystack = " ".join([entry.title, *entry.vision_tags_json, profile_text])
        score = sum(2 if token in entry.title else 1 for token in tokens if token in haystack)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].created_at))
    return [entry for _, entry in scored[:limit]]


def delete_asset_entry(db: Session, asset_id: str) -> None:
    entry = get_asset_entry(db, asset_id)
    LocalStorage().resolve(entry.storage_path).unlink(missing_ok=True)
    db.delete(entry)
    db.commit()


_BUILTIN_SEEDS = (
    {
        "filename": "black-pink-impact-type.jpg",
        "title": "黑粉撞色大字报风",
        "tags": ["大字报", "黑粉撞色", "促销", "冲击力", "美业", "开业"],
        "profile": {
            "summary": "黑粉撞色大字报风格，适合开业酬宾、限时促销类朋友圈海报",
            "layout": "大字标题居中，价格与活动信息在下部",
            "palette": ["黑色", "玫粉"],
            "typography": "黑体冲击力强",
            "copy_slots": "主标题 1、副标题 1、价格区 1",
            "mood": "热烈、紧迫",
            "suitable_categories": ["美容", "美发", "零售促销"],
            "tags": ["大字报", "黑粉撞色", "促销", "冲击力"],
        },
    },
    {
        "filename": "emerald-model-editorial.jpg",
        "title": "翡翠绿模特编辑风",
        "tags": ["模特", "编辑风", "翡翠绿", "高级感", "护理", "轻奢"],
        "profile": {
            "summary": "翡翠绿底模特编辑排版，适合面部护理、轻奢服务的形象海报",
            "layout": "模特主视觉占上 2/3，文案区在底部",
            "palette": ["翡翠绿", "米白"],
            "typography": "衬线与细黑混排，高级克制",
            "copy_slots": "主标题 1、卖点 2",
            "mood": "高级、安静",
            "suitable_categories": ["美容护理", "医美", "轻奢零售"],
            "tags": ["模特", "编辑风", "翡翠绿", "高级感"],
        },
    },
    {
        "filename": "purple-black-event-report.jpg",
        "title": "紫黑活动战报风",
        "tags": ["战报", "紫黑", "活动复盘", "数据", "年终", "冲刺"],
        "profile": {
            "summary": "紫黑渐变战报风，适合业绩战报、活动复盘、冲刺动员",
            "layout": "标题在顶部，数据块网格排布",
            "palette": ["深紫", "黑", "金色点缀"],
            "typography": "数字用窄黑体，强调数据",
            "copy_slots": "标题 1、数据块 3-4",
            "mood": "专业、燃",
            "suitable_categories": ["美业门店", "团队激励", "活动复盘"],
            "tags": ["战报", "紫黑", "数据", "冲刺"],
        },
    },
)


def bootstrap_builtin_assets() -> int:
    """把内置 moments 模板导入素材库（幂等）：启动时调用。"""
    db = get_session_factory()()
    try:
        existing_titles = set(
            db.scalars(select(AssetLibraryEntry.title).where(AssetLibraryEntry.source == "builtin")).all()
        )
        storage = LocalStorage()
        created = 0
        for seed in _BUILTIN_SEEDS:
            if seed["title"] in existing_titles:
                continue
            source_path = _BUILTIN_TEMPLATES_DIR / str(seed["filename"])
            if not source_path.exists():
                continue
            content = source_path.read_bytes()
            with Image.open(source_path) as image:
                width, height = image.size
            entry = AssetLibraryEntry(
                kind="template",
                title=str(seed["title"]),
                source="builtin",
                storage_path=storage.save_library_asset("template", str(seed["filename"]), content),
                mime_type="image/jpeg",
                width=width,
                height=height,
                vision_tags_json=list(seed["tags"]),
                template_profile_json=dict(seed["profile"]),
            )
            db.add(entry)
            created += 1
        if created:
            db.commit()
        return created
    finally:
        db.close()
