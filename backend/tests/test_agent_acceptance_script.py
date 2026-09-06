"""M3 验收：小白用户完整剧本的确定性回放（spec §10）。

剧本：澄清 → 推荐 → 文案 → 出图 → 存档，全程用户只说大白话。
"""

from __future__ import annotations

import json
from pathlib import Path

from helpers import _login  # noqa: F401 - 供后续人工验收脚本复用

from productflow_backend.application.asset_library import bootstrap_builtin_assets
from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall
from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
from productflow_backend.infrastructure.db.session import get_session_factory


def test_full_novice_user_script_end_to_end(configured_env: Path, install_scripted_llm) -> None:
    bootstrap_builtin_assets()
    script = [
        # 第 1 轮：用户一句话需求 → agent 主动推荐方案
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-rec-1",
                    name="recommend_designs",
                    arguments={"requirement": "开业 促销 高级感 战报"},
                )
            ],
        ),
        AgentLLMResponse(content="给你挑了 3 个方案：黑粉大字报最有开业气氛，要我用它继续吗？"),
        # 第 2 轮：用户选中方案 → 出文案
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-copy-1",
                    name="write_copy",
                    arguments={"brief": "美容院开业酬宾，面部护理 199 体验，亲切", "kind": "moments"},
                )
            ],
        ),
        AgentLLMResponse(
            content=json.dumps(
                [{"title": "开业大酬宾", "content": "本周末开业，护理体验 199", "hashtags": ["开业"]}],
                ensure_ascii=False,
            )
        ),
        AgentLLMResponse(content="文案在这，选一版我就开始出图。"),
        # 第 3 轮：用户确认 → 出图
        AgentLLMResponse(
            content=None,
            tool_calls=[
                AgentToolCall(
                    call_id="call-gen-1",
                    name="generate_image",
                    arguments={"prompt": "美容院开业海报，黑粉撞色，文案位在下方", "size": "1080x1440"},
                )
            ],
        ),
        AgentLLMResponse(content="海报出来了！不满意可以让我改，满意的话我帮你存进素材库。"),
        # 第 4 轮：用户满意 → 存档
        AgentLLMResponse(
            content=None,
            tool_calls=[AgentToolCall(call_id="call-save-1", name="save_asset", arguments={})],
        ),
        AgentLLMResponse(content="已收进素材库，下次做同类风格可以直接调用。"),
    ]
    llm = install_scripted_llm(
        script,
        vision_response={"summary": "x", "tags": []},
        utility_responses={
            "recommend_rationales": json.dumps(
                [
                    {"asset_id": "a", "why": "撞色大字报最有开业气氛"},
                    {"asset_id": "b", "why": "编辑风适合护理调性"},
                    {"asset_id": "c", "why": "战报适合活动复盘"},
                ],
                ensure_ascii=False,
            ),
        },
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="开业海报全流程")

        turn1 = run_agent_turn(
            db,
            agent_session_id=agent_session.id,
            user_content="我周末开业，想要个酬宾海报，不懂怎么弄",
        )
        assert agent_session.stage == "recommend"
        recommend_event = turn1.tool_events[0]
        assert recommend_event["tool"] == "recommend_designs"
        assert recommend_event["result"].get("status") == "completed", recommend_event["result"]
        recommendations = recommend_event["result"]["recommendations"]
        assert len(recommendations) == 3
        assert all(item["why"] for item in recommendations)

        turn2 = run_agent_turn(db, agent_session_id=agent_session.id, user_content="就用大字报那个，先给我文案")
        assert agent_session.stage == "produce"
        copy_result = json.loads([m for m in turn2.messages if m.role == "tool"][-1].content)
        assert copy_result["status"] == "completed", copy_result
        assert len(copy_result["copies"]) == 1

        turn3 = run_agent_turn(db, agent_session_id=agent_session.id, user_content="就要这版，出图吧")
        assert agent_session.stage == "review"
        gen_result = json.loads([m for m in turn3.messages if m.role == "tool"][-1].content)
        assert gen_result["status"] == "completed"
        assert gen_result["completed_assets"]

        turn4 = run_agent_turn(db, agent_session_id=agent_session.id, user_content="挺满意的，存一下吧")
        save_events = [event for event in turn4.tool_events if event["tool"] == "save_asset"]
        assert save_events and save_events[0]["result"]["status"] == "completed"

        # 全程用户没有写过任何 prompt 式指令；工具链完整走通
        assert agent_session.image_session_id is not None
        # 8 次外层循环调用（4 轮 × 工具轮+最终轮）+ 3 次内部工具调用
        # （推荐理由、文案生成、存档自动打标）
        assert len(llm.calls) == 11
    finally:
        db.close()
