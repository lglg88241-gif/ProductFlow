"""文案报告落库与下载闭环（功能 E）：围栏剥离、双键兼容、失败兜底与下载路由。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import _login

from productflow_backend.application.designer_agent.llm import AgentLLMResponse, AgentToolCall


def _run_report_turn(install_scripted_llm, utility_response: str) -> tuple[str, dict]:
    """跑一轮 write_copy_report，返回 (agent_session_id, tool_result)。"""
    from productflow_backend.application.designer_agent.loop import create_agent_session, run_agent_turn
    from productflow_backend.infrastructure.db.session import get_session_factory

    install_scripted_llm(
        [
            AgentLLMResponse(
                content=None,
                tool_calls=[
                    AgentToolCall(
                        call_id="call-report-1",
                        name="write_copy_report",
                        arguments={"brief": "美容院周末开业酬宾，面部护理 199 元体验", "tone": "亲切"},
                    )
                ],
            ),
            AgentLLMResponse(content="报告已经整理好了，点下载就能拿走。"),
        ],
        utility_responses={"copy_report": utility_response},
    )
    db = get_session_factory()()
    try:
        agent_session = create_agent_session(db, title="文案报告")
        agent_session_id = agent_session.id
        result = run_agent_turn(db, agent_session_id=agent_session_id, user_content="帮我出一份完整的文案报告")
        tool_result = json.loads([m for m in result.messages if m.role == "tool"][-1].content)
        return agent_session_id, tool_result
    finally:
        db.close()


def test_write_copy_report_parses_content_and_persists(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.infrastructure.db.models import CopyReport
    from productflow_backend.infrastructure.db.session import get_session_factory

    payload = json.dumps(
        {"title": "美容院开业文案报告", "content": "## 朋友圈正文\n本周末开业，199 元体验\n\n## 卖点清单\n- 体验价"},
        ensure_ascii=False,
    )
    agent_session_id, tool_result = _run_report_turn(install_scripted_llm, payload)
    assert tool_result["status"] == "ok", tool_result
    assert tool_result["title"] == "美容院开业文案报告"
    assert tool_result["download_url"] == f"/api/agent/copy-reports/{tool_result['report_id']}/download"
    assert tool_result["preview"].startswith("## 朋友圈正文")

    db = get_session_factory()()
    try:
        report = db.get(CopyReport, tool_result["report_id"])
        assert report is not None
        assert report.agent_session_id == agent_session_id
        assert report.title == "美容院开业文案报告"
        assert "## 卖点清单" in report.content_md
    finally:
        db.close()


def test_write_copy_report_strips_code_fence(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.infrastructure.db.models import CopyReport
    from productflow_backend.infrastructure.db.session import get_session_factory

    inner = json.dumps({"title": "围栏报告", "content": "正文内容A"}, ensure_ascii=False)
    fenced = f"```json\n{inner}\n```"
    _, tool_result = _run_report_turn(install_scripted_llm, fenced)
    assert tool_result["status"] == "ok", tool_result
    assert tool_result["title"] == "围栏报告"
    db = get_session_factory()()
    try:
        report = db.get(CopyReport, tool_result["report_id"])
        assert report is not None
        assert report.content_md == "正文内容A"
    finally:
        db.close()


def test_write_copy_report_accepts_sections_key(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.infrastructure.db.models import CopyReport
    from productflow_backend.infrastructure.db.session import get_session_factory

    payload = json.dumps(
        {
            "title": "分节报告",
            "sections": [
                {"heading": "朋友圈正文", "body": "开业啦"},
                {"heading": "发布建议", "body": "周六早上发"},
            ],
        },
        ensure_ascii=False,
    )
    _, tool_result = _run_report_turn(install_scripted_llm, payload)
    assert tool_result["status"] == "ok", tool_result
    db = get_session_factory()()
    try:
        report = db.get(CopyReport, tool_result["report_id"])
        assert report is not None
        assert "## 朋友圈正文\n开业啦" in report.content_md
        assert "## 发布建议\n周六早上发" in report.content_md
    finally:
        db.close()


def test_write_copy_report_falls_back_to_plain_text_on_parse_failure(
    configured_env: Path,
    install_scripted_llm,
) -> None:
    """解析失败也落一份纯文本报告，保证用户拿得到产物。"""
    from productflow_backend.infrastructure.db.models import CopyReport
    from productflow_backend.infrastructure.db.session import get_session_factory

    raw = "这不是 JSON，就是一段普通文案。"
    _, tool_result = _run_report_turn(install_scripted_llm, raw)
    assert tool_result["status"] == "ok", tool_result
    assert tool_result["preview"] == raw
    db = get_session_factory()()
    try:
        report = db.get(CopyReport, tool_result["report_id"])
        assert report is not None
        assert report.content_md == raw
        assert report.title  # 回退标题非空
    finally:
        db.close()


def test_copy_report_download_route_returns_markdown_attachment(configured_env: Path, install_scripted_llm) -> None:
    from productflow_backend.presentation.api import create_app

    payload = json.dumps({"title": "美容院开业文案报告", "content": "# 报告\n正文"}, ensure_ascii=False)
    agent_session_id, tool_result = _run_report_turn(install_scripted_llm, payload)

    app = create_app()
    client = TestClient(app)
    assert client.get("/api/agent/copy-reports").status_code == 401
    _login(client)

    response = client.get(tool_result["download_url"])
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    disposition = response.headers["content-disposition"]
    assert "attachment" in disposition
    assert "filename*=utf-8''" in disposition
    assert response.content.decode("utf-8") == "# 报告\n正文"

    missing = client.get("/api/agent/copy-reports/no-such-report/download")
    assert missing.status_code == 404

    listed = client.get("/api/agent/copy-reports", params={"session_id": agent_session_id})
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == tool_result["report_id"]
    assert items[0]["download_url"] == tool_result["download_url"]
