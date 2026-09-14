"""朋友圈分格导出（P2-9）：切片服务与下载端点。"""

from __future__ import annotations

import io
import json
import time
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _login
from PIL import Image

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall
from productflow_backend.application.grid_export import slice_into_grid


def _png_bytes(width: int = 1080, height: int = 1080) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 60, 120)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_slice_into_grid_produces_ordered_tiles() -> None:
    result = slice_into_grid(_png_bytes(1080, 1080), grid="3x3")
    assert result.tile_count == 9
    assert result.tile_size == (360, 360)
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        names = archive.namelist()
        assert names == [f"{index:02d}.png" for index in range(1, 10)]
        with Image.open(io.BytesIO(archive.read("01.png"))) as tile:
            assert tile.size == (360, 360)


def test_slice_handles_non_divisible_sizes() -> None:
    """非整除尺寸下最后一行/列吃掉余数，不丢像素。"""
    result = slice_into_grid(_png_bytes(1000, 1000), grid="3x3")
    with zipfile.ZipFile(io.BytesIO(result.archive)) as archive:
        with Image.open(io.BytesIO(archive.read("09.png"))) as last_tile:
            assert last_tile.size == (1000 - 2 * 333, 1000 - 2 * 333)


def test_slice_rejects_unsupported_grid_and_format() -> None:
    import pytest

    from productflow_backend.domain.errors import BusinessError

    with pytest.raises(BusinessError):
        slice_into_grid(_png_bytes(), grid="5x5")
    with pytest.raises(BusinessError):
        slice_into_grid(_png_bytes(), grid="3x3", fmt="gif")


def test_grid_export_endpoint_returns_zip(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)

    uploaded = client.post(
        "/api/agent/assets",
        files={"file": ("海报.png", _png_bytes(), "image/png")},
        data={"kind": "output"},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["id"]

    exported = client.get(f"/api/agent/assets/{asset_id}/grid-export", params={"grid": "3x3"})
    assert exported.status_code == 200
    assert exported.headers["content-type"] == "application/zip"
    assert exported.headers["x-grid-tiles"] == "9"
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert len(archive.namelist()) == 9

    bad_grid = client.get(f"/api/agent/assets/{asset_id}/grid-export", params={"grid": "9x9"})
    assert bad_grid.status_code == 400


def test_export_tool_returns_download_link(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-exp-1",
                        name="export_moments_grid",
                        arguments={"grid": "3x3"},
                    )
                ],
            ),
            AgentLLMResponse(content="九宫格切好了，点下载就能发啦。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="帮我切成九宫格发朋友圈")
        tool_result = json.loads([m for m in result.messages if m.role == "tool"][-1].content)
        # 没有产出时给出人话提示（不是崩溃）
        assert tool_result["status"] == "error"
        assert "先做一张" in tool_result["message"]
    finally:
        db.close()


def test_product_pipeline_tool_submits_and_completes(configured_env: Path, install_scripted_llm) -> None:
    """P2-10 收编：run_product_pipeline 提交流水线，check_pipeline_status 查询产出。"""
    from productflow_backend.application.asset_library import register_asset_upload
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.application.product_workflows import get_product_workflow_status
    from productflow_backend.infrastructure.db.models import Product
    from productflow_backend.infrastructure.db.session import get_session_factory

    db = get_session_factory()()
    try:
        product_asset = register_asset_upload(
            db, kind="reference", filename="商品图.png", content=_png_bytes(900, 900)
        )
        install_scripted_llm(
            [
                AgentLLMResponse(
                    content=None,
                    tool_calls=[
                        AgentToolCall(
                            call_id="call-pipe-1",
                            name="run_product_pipeline",
                            arguments={
                                "product_name": "流水线护手霜",
                                "library_asset_id": product_asset.id,
                                "category": "个护",
                                "price": "49.90",
                            },
                        )
                    ],
                ),
                AgentLLMResponse(content="流水线已启动，完成后我来汇报。"),
            ]
        )
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="用这张商品图批量做一套素材")

        pipe_result = json.loads([m for m in result.messages if m.role == "tool"][-1].content)
        assert pipe_result["status"] == "submitted", pipe_result
        product_id = pipe_result["product_id"]
        assert pipe_result["run_status"] in {"queued", "running", "succeeded"}

        # 等待内联执行的工作流完成（与 E2E 等待模式一致）
        deadline = time.time() + 60
        final_status = None
        while time.time() < deadline:
            workflow_state = get_product_workflow_status(db, product_id)
            runs = workflow_state.runs
            final_status = runs[0].status if runs else None
            if final_status in {"succeeded", "failed", "cancelled"}:
                break
            db.expire_all()
            time.sleep(0.2)
        node_dump = [(getattr(n, "node_type", "?"), str(getattr(n, "status", None))) for n in workflow_state.nodes]
        assert final_status == "succeeded", f"工作流未完成: {final_status} | 节点: {node_dump}"

        # 第二轮：查询状态与产出
        install_scripted_llm(
            [
                AgentLLMResponse(
                    content=None,
                    tool_calls=[
                        AgentToolCall(
                            call_id="call-chk-1",
                            name="check_pipeline_status",
                            arguments={"product_id": product_id},
                        )
                    ],
                ),
                AgentLLMResponse(content="流水线跑完了，海报都出来了。"),
            ]
        )
        check = run_agent_turn(db, agent_session_id=agent_session.id, user_content="查一下流水线结果")

        check_result = json.loads([m for m in check.messages if m.role == "tool"][-1].content)
        assert check_result["run_status"] == "succeeded", check_result
        assert check_result["poster_count"] >= 1

        product = db.get(Product, product_id)
        assert product is not None
        assert product.name == "流水线护手霜"
    finally:
        db.close()


def test_product_pipeline_requires_library_asset(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-pipe-2",
                        name="run_product_pipeline",
                        arguments={"product_name": "没有图的商品"},
                    )
                ],
            ),
            AgentLLMResponse(content="需要商品图哦。"),
        ]
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db)
        result = run_agent_turn(db, agent_session_id=agent_session.id, user_content="做个商品图套图")
        tool_result = json.loads([m for m in result.messages if m.role == "tool"][-1].content)
        assert tool_result["status"] == "error"
        assert "商品主图" in tool_result["message"]
    finally:
        db.close()
