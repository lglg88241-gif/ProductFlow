"""设计师素材库（M2）：上传登记、内置样板冷启动、检索与 agent 工具集成。"""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _enable_deletion, _login
from PIL import Image

from productflow_backend.application.asset_library import get_asset_entry
from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall


def _png_bytes(width: int = 640, height: int = 800) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (40, 20, 60)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_builtin_assets_bootstrap_is_idempotent(configured_env: Path) -> None:
    from productflow_backend.application.asset_library import bootstrap_builtin_assets, list_asset_entries
    from productflow_backend.infrastructure.db.session import get_session_factory

    first = bootstrap_builtin_assets()
    second = bootstrap_builtin_assets()
    assert first == 3
    assert second == 0

    db = get_session_factory()()
    try:
        entries = list_asset_entries(db, kind="template")
        builtin = [entry for entry in entries if entry.source == "builtin"]
        assert len(builtin) == 3
        assert all(entry.template_profile_json for entry in builtin)
        assert all(entry.vision_tags_json for entry in builtin)
    finally:
        db.close()


def test_agent_search_finds_builtin_template_by_scene(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.asset_library import bootstrap_builtin_assets
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    bootstrap_builtin_assets()
    script = [
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(call_id="call-search-1", name="search_assets", arguments={"query": "开业 促销"})
            ],
        ),
        AgentLLMResponse(content="素材库里有一套黑粉撞色大字报样板，很适合开业促销，要用它出图吗？"),
    ]
    install_scripted_llm(script)
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="周末开业，要个促销海报")

        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "completed"
        assert tool_result["message"].startswith("找到")
        titles = " ".join(match["title"] for match in tool_result["matches"])
        assert "大字报" in titles
    finally:
        db.close()


def test_analyze_template_tool_builds_profile(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.asset_library import register_asset_upload
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        entry = register_asset_upload(db, kind="template", filename="ziding.png", content=_png_bytes())
        llm = install_scripted_llm(
            [
                AgentLLMResponse(
                    content=None,
                    tool_calls=[
                        AgentToolCall(call_id="call-ana-1", name="analyze_template", arguments={"asset_id": entry.id})
                    ],
                ),
                AgentLLMResponse(content="这张模板我读完了：紫底促销风，适合开业。要按这个风格给你出图吗？"),
            ],
            vision_response={
                "summary": "紫底促销模板",
                "layout": "上图下文",
                "palette": ["紫", "白"],
                "typography": "黑体",
                "copy_slots": "标题 1、价格 1",
                "mood": "热烈",
                "suitable_categories": ["美容"],
                "tags": ["促销", "紫底", "开业"],
            },
        )
        agent_session = create_agent_session(db)
        result = run_agent_turn(
            db,
            agent_session_id=agent_session.id,
            user_content="我上传了一张模板，帮我看看它是什么风格",
        )

        assert llm.image_calls >= 1
        tool_result = json.loads(result.messages[2].content)
        assert tool_result["status"] == "completed"
        assert tool_result["template_profile"]["layout"] == "上图下文"
        assert "促销" in tool_result["tags"]

        db.expire_all()
        refreshed = get_asset_entry(db, entry.id)
        assert refreshed.template_profile_json is not None
    finally:
        db.close()


def test_asset_routes_upload_list_download_delete(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    assert client.get("/api/agent/assets").status_code == 401
    _login(client)

    uploaded = client.post(
        "/api/agent/assets",
        files={"file": ("模板.png", _png_bytes(), "image/png")},
        data={"kind": "template"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["kind"] == "template"
    assert asset["download_url"].startswith("/api/agent/assets/")

    listed = client.get("/api/agent/assets", params={"kind": "template"})
    assert listed.status_code == 200
    assert any(item["id"] == asset["id"] for item in listed.json()["items"])

    thumbnail = client.get(asset["download_url"], params={"variant": "thumbnail"})
    assert thumbnail.status_code == 200
    with Image.open(BytesIO(thumbnail.content)) as thumb:
        assert max(thumb.size) <= 320

    bad_kind = client.get("/api/agent/assets", params={"kind": "nope"})
    assert bad_kind.status_code == 400

    assert client.delete(f"/api/agent/assets/{asset['id']}").status_code == 403
    _enable_deletion(client)
    assert client.delete(f"/api/agent/assets/{asset['id']}").status_code == 204


def test_save_asset_tool_archives_generated_image(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-gen-1",
                        name="generate_image",
                        arguments={"prompt": "紫色开业海报", "size": "1024x1024"},
                    )
                ],
            ),
            AgentLLMResponse(content="图片生成好了！要存进素材库吗？"),
            AgentLLMResponse(
                content=None,
                tool_calls=[AgentToolCall(call_id="call-save-1", name="save_asset", arguments={})],
            ),
            AgentLLMResponse(content="这张已经收进素材库了，下次做类似风格可以直接调用。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        # 生成
        first = run_agent_turn(db, agent_session_id=agent_session.id, user_content="来一张紫色开业海报")
        generated = json.loads(first.messages[2].content)
        asset_id = generated["completed_assets"][0]["asset_id"]
        # 保存（第二个工具调用没有参数，executor 从会话最新产出兜底）
        second = run_agent_turn(db, agent_session_id=agent_session.id, user_content="不错，存到素材库")

        save_events = [event for event in second.tool_events if event["tool"] == "save_asset"]
        assert save_events, "应执行 save_asset 工具"
        save_result = save_events[0]["result"]
        assert save_result["status"] == "completed", save_result
        assert save_result["kind"] == "output"

        from productflow_backend.application.asset_library import list_asset_entries

        outputs = [entry for entry in list_asset_entries(db, kind="output") if entry.source == "generated"]
        assert outputs, "素材库应出现 generated 成品"
        assert any(entry.image_session_id == agent_session.image_session_id for entry in outputs)
        assert asset_id
    finally:
        db.close()
