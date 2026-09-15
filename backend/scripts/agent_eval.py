"""Agent 质量评测（golden set）：用真实模型跑一批"小白会说的话"，给路由与话术打分。

为什么需要它：单元测试用剧本化假 LLM，只能证明"管线没坏"，证明不了
"真实模型是否选对工具、是否守住澄清预算、是否把技术细节漏给用户"。
改提示词或工具描述前，必须先能测出变化——这个脚本就是那把尺子。

设计：
  · 每个场景是一句自然语言需求 + 期望（该用哪些工具 / 绝不能用哪些工具 / 最多问几次）。
  · 走真实 API（含 nginx 反代）与非流式端点，直接读 tool_events，无需解析 SSE。
  · 默认只跑「便宜档」场景（不触发真实生图，不烧生图额度）；--include-costly 打开全量。
  · 结束时输出计分板并按门槛返回退出码（--min-score，默认 0.8）。

用法：
    uv run --directory backend python scripts/agent_eval.py \
        --base-url http://127.0.0.1:29281 --admin-key <ADMIN_ACCESS_KEY>
    uv run --directory backend python scripts/agent_eval.py --include-costly --min-score 0.7
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

import httpx

# 用户可见文本里绝不允许出现的痕迹（技术细节、密钥、URL、错误码）
LEAK_PATTERNS = (
    r"Error code",
    r"Traceback",
    r"https?://",
    r"sk-[A-Za-z0-9]",
    r"AGENT_[A-Z_]+",
    r"InternalServerError",
    r"Cloudflare",
    r"api_key",
)

GENERATION_TOOLS = {"generate_image", "edit_image", "run_product_pipeline"}


@dataclass
class Scenario:
    sid: str
    message: str
    expect_any: set[str] = field(default_factory=set)
    forbid: set[str] = field(default_factory=set)
    max_questions: int = 1
    costly: bool = False
    note: str = ""


@dataclass
class Outcome:
    scenario: Scenario
    tools: list[str] = field(default_factory=list)
    failed_tools: list[str] = field(default_factory=list)
    questions: int = 0
    leaks: list[str] = field(default_factory=list)
    latency_s: float = 0.0
    error: str = ""
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.error and not self.failures


SCENARIOS: list[Scenario] = [
    Scenario(
        sid="copy-report",
        message="帮我写一份开业活动文案报告，要能下载的那种，我们店是做面部护理的",
        expect_any={"write_copy_report"},
        forbid=GENERATION_TOOLS,
        note="成体系的报告应走 report 而非短文案",
    ),
    Scenario(
        sid="template-lookup",
        message="你们有什么现成的模板可以参考吗？我想看看风格",
        expect_any={"search_assets", "recommend_designs"},
        forbid=GENERATION_TOOLS,
        note="只是问参考，不该直接生成",
    ),
    Scenario(
        sid="moments-grid",
        message="我要发朋友圈，帮我把图切成九宫格那种多图形式",
        expect_any={"export_moments_grid"},
        note="明确提到发朋友圈多图 → 必须用分格导出",
    ),
    Scenario(
        sid="text-only-edit",
        message="这个商品海报我只想把价格改成 49.9，其他都不要动",
        expect_any={"rerender_poster_copy"},
        forbid={"generate_image", "edit_image"},
        note="只改文字 → 本地重渲，绝不该重新画图（省额度且更快）",
    ),
    Scenario(
        sid="vague-brief",
        message="我周末开业，想要个酬宾海报，完全不懂怎么弄",
        expect_any={
            "recommend_designs",
            "write_copy",
            "search_assets",
            "analyze_template",
        },
        note="模糊需求必须动手（推荐/文案/检索），而不是纯聊天；不含生图以保持默认档免费",
    ),
    Scenario(
        sid="label-quantity",
        message="就要一张 1080x1440 的开业海报，我只要一张",
        expect_any={"generate_image"},
        costly=True,
        note="用户明确数量时按数量出",
    ),
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agent 质量评测（golden set）")
    parser.add_argument("--base-url", default="http://127.0.0.1:29281")
    parser.add_argument("--admin-key", required=True)
    parser.add_argument("--include-costly", action="store_true", help="包含会真实生图的场景（消耗生图额度）")
    parser.add_argument("--min-score", type=float, default=0.8, help="低于该通过率则以非零码退出")
    parser.add_argument("--keep-sessions", action="store_true", help="保留评测会话便于人工查看")
    return parser.parse_args()


def _login(client: httpx.Client, admin_key: str) -> None:
    response = client.post("/api/auth/session", json={"admin_key": admin_key})
    response.raise_for_status()


def _count_questions(messages: list[dict]) -> int:
    """统计助手主动提问的次数（以问号结尾算一次追问）。"""
    return sum(
        1
        for message in messages
        if message.get("role") == "assistant" and str(message.get("content", "")).rstrip().endswith(("？", "?"))
    )


def _run_scenario(client: httpx.Client, scenario: Scenario) -> Outcome:
    import time

    outcome = Outcome(scenario=scenario)
    session_id = ""
    try:
        created = client.post("/api/agent/sessions", json={"title": f"评测·{scenario.sid}"})
        created.raise_for_status()
        session_id = created.json()["id"]

        start = time.perf_counter()
        response = client.post(
            f"/api/agent/sessions/{session_id}/messages",
            json={"content": scenario.message},
        )
        outcome.latency_s = time.perf_counter() - start
        if response.status_code == 503:
            detail = ""
            try:
                detail = str(response.json().get("detail", ""))
            except Exception:  # noqa: BLE001
                pass
            outcome.error = f"SKIP：模型调用失败（503）{detail[:80]}"
            return outcome
        response.raise_for_status()
        payload = response.json()
        events = payload.get("tool_events", [])
        outcome.tools = [event["tool"] for event in events]
        # 只看工具名会把"调了但执行失败"算成通过——这里记录失败的工具
        outcome.failed_tools = [
            event["tool"]
            for event in events
            if str((event.get("result") or {}).get("status", "")).lower() in {"error", "failed"}
        ]

        detail = client.get(f"/api/agent/sessions/{session_id}").json()
        messages = detail.get("messages", [])
        outcome.questions = _count_questions(messages)

        visible = "\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "assistant"
        )
        outcome.leaks = [pattern for pattern in LEAK_PATTERNS if re.search(pattern, visible)]

        # 判定
        if scenario.expect_any and not (set(outcome.tools) & scenario.expect_any):
            outcome.failures.append(
                f"期望调用 {sorted(scenario.expect_any)}，实际 {outcome.tools or '无工具调用'}"
            )
        forbidden_hit = set(outcome.tools) & scenario.forbid
        if forbidden_hit:
            outcome.failures.append(f"不该调用 {sorted(forbidden_hit)}")
        expected_hit = set(outcome.tools) & scenario.expect_any
        if expected_hit and expected_hit <= set(outcome.failed_tools):
            outcome.failures.append(f"期望的工具执行失败: {sorted(expected_hit)}")
        if outcome.failed_tools and not expected_hit:
            outcome.failures.append(f"工具执行失败: {sorted(set(outcome.failed_tools))}")
        if outcome.questions > scenario.max_questions:
            outcome.failures.append(f"追问 {outcome.questions} 次，超出澄清预算 {scenario.max_questions}")
        if outcome.leaks:
            outcome.failures.append(f"用户可见文本泄露技术细节: {outcome.leaks}")
    except Exception as exc:  # noqa: BLE001 - 评测脚本需要记录任何失败而不中断整批
        outcome.error = f"{type(exc).__name__}: {exc}"
    finally:
        if session_id and not _KEEP_SESSIONS:
            client.delete(f"/api/agent/sessions/{session_id}")
    return outcome


_KEEP_SESSIONS = False


def main() -> None:
    global _KEEP_SESSIONS
    args = _parse_args()
    _KEEP_SESSIONS = args.keep_sessions

    with httpx.Client(base_url=args.base_url, timeout=420.0) as client:
        _login(client, args.admin_key)
        before = client.get("/api/metrics/summary").json()

        selected = [s for s in SCENARIOS if args.include_costly or not s.costly]
        skipped = [s for s in SCENARIOS if s not in selected]
        print(f"■ Agent 质量评测：{len(selected)} 个场景"
              f"{f'（跳过 {len(skipped)} 个消耗生图额度的场景）' if skipped else ''}\n")

        outcomes: list[Outcome] = []
        for index, scenario in enumerate(selected):
            outcome = _run_scenario(client, scenario)
            outcomes.append(outcome)
            mark = "✔" if outcome.passed else ("⊘" if outcome.error.startswith("SKIP") else "✘")
            detail = outcome.error or "；".join(outcome.failures) or f"工具={outcome.tools}"
            print(f"  {mark} {scenario.sid:18s} {outcome.latency_s:6.1f}s  {detail}")
            # 预检：第一个场景就因模型不可用而 SKIP 时，整批都会失败——立即停下并给诊断，
            # 而不是白等 5 个场景（首场景可能耗时数分钟）
            if index == 0 and outcome.error.startswith("SKIP") and "503" in outcome.error:
                print("\n首场景即模型不可用，已中止。请检查后端日志里的供应商报错，"
                      "常见原因：中转站下线了 .env 里配置的模型（改名/下架），"
                      "或主备两个模型都不可用。")
                print("排查：docker compose logs productflow-backend | grep '设计师模型调用失败'")
                sys.exit(2)

        after = client.get("/api/metrics/summary").json()
        scored = [o for o in outcomes if not o.error.startswith("SKIP")]
        passed = [o for o in scored if o.passed]
        score = len(passed) / len(scored) if scored else 0.0
        tokens_used = max(0, int(after.get("total_tokens", 0)) - int(before.get("total_tokens", 0)))
        avg_latency = sum(o.latency_s for o in scored) / len(scored) if scored else 0.0

        print("\n" + "=" * 68)
        print(f"通过率: {len(passed)}/{len(scored)} = {score:.0%}    门槛: {args.min_score:.0%}")
        print(f"本轮消耗 tokens: {tokens_used}    平均耗时: {avg_latency:.1f}s")
        if skipped:
            print(f"未覆盖（需 --include-costly）: {[s.sid for s in skipped]}")
        print("=" * 68)

        if scored and score < args.min_score:
            print("\n未达门槛。建议排查：提示词路由规则、工具描述三段式、澄清预算约束。")
            sys.exit(1)


if __name__ == "__main__":
    main()
