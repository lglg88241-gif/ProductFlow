"""工具 schema 描述质量的回归测试（实习生文档标准）。

防止工具描述退化回一句话：每个工具必须说清【做什么】/【何时用】/【何时不用】，
关键数值参数必须有 min/max 或 enum 约束，让 LLM 知道什么时候该用哪个工具。
"""

from __future__ import annotations

import pytest

from productflow_backend.application.designer_agent.tools import tool_schemas


def _schema_by_name() -> dict[str, dict]:
    schemas = tool_schemas()
    assert schemas, "tool_schemas 不能为空"
    return {item["function"]["name"]: item["function"] for item in schemas}


def test_every_tool_description_documents_when_to_use() -> None:
    """每个工具描述非空、含'何时'标记、并具备三段式结构（做什么/何时用/何时不用）。"""
    for name, function in _schema_by_name().items():
        description = function.get("description", "")
        assert description.strip(), f"工具 {name} 的 description 为空"
        assert "何时" in description, f"工具 {name} 的描述缺少'何时用'触发词说明"
        assert len(description) >= 30, f"工具 {name} 的描述过短（{len(description)} 字符），退化为一句话"
        for marker in ("【做什么】", "【何时用】", "【何时不用】"):
            assert marker in description, f"工具 {name} 的描述缺少 {marker} 段落"


@pytest.mark.parametrize(
    "name",
    [
        "generate_image",
        "edit_image",
        "write_copy",
        "write_copy_report",
        "search_assets",
        "analyze_template",
        "recommend_designs",
        "export_moments_grid",
        "run_product_pipeline",
        "check_pipeline_status",
        "save_asset",
        "rerender_poster_copy",
    ],
)
def test_core_tools_present(name: str) -> None:
    """核心工具不缺位：防止重写 schema 时误删工具。"""
    assert name in _schema_by_name()


def test_generate_image_count_is_bounded() -> None:
    """generate_image 的 count 参数必须带 1~4 的数值约束。"""
    count = _schema_by_name()["generate_image"]["parameters"]["properties"]["count"]
    assert count.get("minimum") == 1
    assert count.get("maximum") == 4


def test_export_moments_grid_grid_is_enum() -> None:
    """export_moments_grid 的 grid 参数必须是枚举，收敛分格方式。"""
    grid = _schema_by_name()["export_moments_grid"]["parameters"]["properties"]["grid"]
    assert grid.get("type") == "string"
    assert list(grid.get("enum", [])) == ["3x3", "2x2", "3x1", "1x3"]


def test_disambiguation_between_write_copy_and_report() -> None:
    """write_copy 与 write_copy_report 的描述要互相划清边界（短文案 vs 报告文档）。"""
    schemas = _schema_by_name()
    assert "write_copy_report" in schemas["write_copy"]["description"]
    assert "write_copy" in schemas["write_copy_report"]["description"]


def test_edit_image_documented_asset_id_reuse() -> None:
    """edit_image 描述必须说明把上一轮 asset_id 传回以保持画面一致。"""
    description = _schema_by_name()["edit_image"]["description"]
    assert "asset_id" in description


def test_rerender_poster_copy_routes_away_from_redraw() -> None:
    """rerender_poster_copy 描述要指明改画面时应转用 edit_image/generate_image。"""
    description = _schema_by_name()["rerender_poster_copy"]["description"]
    assert "edit_image" in description and "generate_image" in description
