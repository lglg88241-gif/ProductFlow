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


def test_registry_schema_and_stage_maps_stay_in_sync() -> None:
    """工具脊柱契约：schema / 执行器 / 阶段映射三者必须一一对应。

    这是"加工具只需登记一次"的护栏——任何一处漏登记都会在这里失败，
    而不是等到线上发现模型调了个不存在的工具、或阶段徽章不动。
    """
    from productflow_backend.application.designer_agent.prompts import STAGE_BY_TOOL, VALID_STAGES
    from productflow_backend.application.designer_agent.tools import tool_names, tool_schemas

    schema_names = {item["function"]["name"] for item in tool_schemas()}
    handler_names = set(tool_names())

    assert schema_names == handler_names, {
        "有 schema 无执行器": sorted(schema_names - handler_names),
        "有执行器无 schema": sorted(handler_names - schema_names),
    }

    missing_stage = schema_names - set(STAGE_BY_TOOL)
    assert not missing_stage, f"这些工具缺少阶段映射（阶段徽章会失真）: {sorted(missing_stage)}"
    assert set(STAGE_BY_TOOL) <= schema_names, sorted(set(STAGE_BY_TOOL) - schema_names)
    invalid = {name: stage for name, stage in STAGE_BY_TOOL.items() if stage not in VALID_STAGES}
    assert not invalid, f"阶段值不在 VALID_STAGES 内: {invalid}"


def test_tool_schemas_module_is_single_source_for_constants() -> None:
    """导出枚举与多候选上下限只应有一处定义（历史上重复三处）。"""
    import productflow_backend.application.designer_agent.tool_schemas as schemas
    import productflow_backend.application.designer_agent.tools as tools

    assert tools.SUPPORTED_EXPORT_GRIDS is schemas.SUPPORTED_EXPORT_GRIDS
    assert tools.GENERATION_COUNT_MAX == schemas.GENERATION_COUNT_MAX

    grid_schema = next(
        item for item in schemas.TOOL_SCHEMAS if item["function"]["name"] == "export_moments_grid"
    )
    enum = grid_schema["function"]["parameters"]["properties"]["grid"]["enum"]
    assert tuple(enum) == schemas.SUPPORTED_EXPORT_GRIDS
