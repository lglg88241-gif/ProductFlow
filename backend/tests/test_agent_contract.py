"""Agent 行为契约：把"每轮固定开销"和"工具契约质量"变成可测量、可回归的门禁。

为什么需要它：提示词与工具 schema 的每一次改动都会影响模型的选择质量与每轮成本，
而单元测试只能证明"代码没坏"，证明不了"agent 还选得对/还没变贵"。这里给出两条硬约束：

1. 预算棘轮 —— 系统提示词 + 全部工具 schema 的估算 token 必须低于阈值。
   基线（2026-09-15 实测）：提示词 ≈1601 + schema ≈3447 = ≈5048 tokens/轮。
   阈值 6000 留约 19% 余量：正常迭代无感，一旦悄悄膨胀就会在此失败，
   逼改动者显式决定"这个工具描述值得多花这些 token 吗"。
2. 单工具上限 —— 单个工具的 schema 不得超过 600 tokens，防止某个工具的描述失控。

估算方式：中日韩字符按 1 token/字，其余按 4 字符/token——量级足够，且不依赖分词库。
"""

from __future__ import annotations

import json

# 每轮固定开销上限（提示词 + 全部工具 schema），单位：估算 token
TURN_OVERHEAD_TOKEN_BUDGET = 6000
# 单个工具 schema 上限
SINGLE_TOOL_TOKEN_BUDGET = 600


def _estimate_tokens(text: str) -> int:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk + (len(text) - cjk) // 4


def _turn_overhead() -> tuple[int, int, list[tuple[int, str]]]:
    from productflow_backend.application.designer_agent.prompts import AGENT_SYSTEM_PROMPT
    from productflow_backend.application.designer_agent.tools import tool_schemas

    prompt_tokens = _estimate_tokens(AGENT_SYSTEM_PROMPT)
    per_tool = sorted(
        (
            (_estimate_tokens(json.dumps(item, ensure_ascii=False)), item["function"]["name"])
            for item in tool_schemas()
        ),
        reverse=True,
    )
    return prompt_tokens, sum(tokens for tokens, _ in per_tool), per_tool


def test_turn_overhead_stays_within_budget() -> None:
    """每轮固定开销（提示词 + 全部工具 schema）不得超过预算棘轮。"""
    prompt_tokens, schema_tokens, per_tool = _turn_overhead()
    total = prompt_tokens + schema_tokens

    assert total <= TURN_OVERHEAD_TOKEN_BUDGET, (
        f"每轮固定开销 {total} tokens 超出预算 {TURN_OVERHEAD_TOKEN_BUDGET}。"
        f"（提示词 {prompt_tokens} + schema {schema_tokens}）"
        f"最贵的工具：{per_tool[:5]}。"
        "请瘦身工具描述或收紧参数结构，确有必要的扩预算需在测试里显式上调并说明原因。"
    )


def test_no_single_tool_schema_bloats() -> None:
    """单个工具的 schema 不得超上限——防止某个工具描述失控。"""
    _, _, per_tool = _turn_overhead()
    bloated = [(name, tokens) for tokens, name in per_tool if tokens > SINGLE_TOOL_TOKEN_BUDGET]
    assert not bloated, f"这些工具的 schema 超过 {SINGLE_TOOL_TOKEN_BUDGET} tokens: {bloated}"


def test_schema_parameter_shapes_are_tight() -> None:
    """参数结构必须收敛：枚举值要么给 enum、要么给明确默认值，避免模型乱填。"""
    from productflow_backend.application.designer_agent.tools import tool_schemas

    for item in tool_schemas():
        fn = item["function"]
        params = fn["parameters"]
        assert params.get("type") == "object", fn["name"]
        for prop_name, prop in params.get("properties", {}).items():
            # 布尔/数组等自由形态允许没有 enum，但枚举型参数必须显式列出取值
            if prop.get("type") == "string" and prop_name in {"grid", "kind", "poster_kind"}:
                assert "enum" in prop, f"{fn['name']}.{prop_name} 缺少 enum 收敛"
