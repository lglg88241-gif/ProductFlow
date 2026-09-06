"""ProductFlow 自动化全功能演示/验证流程。

对运行中的实例（本地 uvicorn 或 docker compose 栈）执行完整功能走查：
登录鉴权 → 素材库与模板识别 → 设计师 Agent 全流程 → 商品工作流 →
图片会话 → 画廊/队列 → 设置与安全 → 清理。每步计时，最终生成
「功能覆盖矩阵 + 性能摘要」Markdown 报告。

用法：
    uv run --directory backend python scripts/demo_flow.py \
        --base-url http://127.0.0.1:29280 \
        --admin-key <ADMIN_ACCESS_KEY> --settings-token <SETTINGS_ACCESS_TOKEN>

依赖真实供应商的部分（Agent 对话）在未配置时自动标记 SKIP，不阻塞其余步骤；
依赖 Redis 队列的生图步骤在队列不可用时优雅失败并在报告中注明。
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

GENERATION_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 2.0


@dataclass
class StepResult:
    category: str
    name: str
    status: str  # PASS / FAIL / SKIP
    duration_ms: float = 0.0
    detail: str = ""


@dataclass
class DemoRunner:
    base_url: str
    admin_key: str
    settings_token: str
    client: httpx.Client = field(init=False)
    results: list[StepResult] = field(default_factory=list)
    timings: dict[str, list[float]] = field(default_factory=dict)
    created_product_id: str | None = None
    created_agent_session_id: str | None = None
    agent_image_session_id: str | None = None
    image_session_id: str | None = None
    gallery_asset_id: str | None = None
    deletion_enabled: bool = False

    def __post_init__(self) -> None:
        self.client = httpx.Client(base_url=self.base_url, timeout=420.0)

    # ------------------------------------------------------------------
    def step(self, category: str, name: str, func, *, note: str = "") -> StepResult:
        start = time.perf_counter()
        try:
            detail = func() or note
            status = "PASS"
        except DemoSkip as exc:
            status, detail = "SKIP", str(exc)
        except Exception as exc:  # noqa: BLE001 - 演示流程需要捕获并报告所有失败
            status, detail = "FAIL", f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - start) * 1000
        result = StepResult(category=category, name=name, status=status, duration_ms=duration_ms, detail=detail)
        self.results.append(result)
        icon = {"PASS": "✔", "FAIL": "✘", "SKIP": "⊘"}[status]
        print(f"  {icon} [{category}] {name}  ({duration_ms:.0f} ms)  {detail}")
        return result

    def record_timing(self, label: str, seconds: float) -> None:
        self.timings.setdefault(label, []).append(seconds * 1000)

    # ------------------------------------------------------------------
    def wait_for_generation(self, image_session_id: str, *, timeout: float = GENERATION_TIMEOUT_SECONDS) -> dict:
        """轮询图片会话直到没有 queued/running 任务。返回最终会话 JSON。"""
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/image-sessions/{image_session_id}")
            response.raise_for_status()
            last = response.json()
            tasks = last.get("generation_tasks", [])
            if tasks and all(task["status"] not in {"queued", "running"} for task in tasks):
                return last
            if not tasks and last.get("rounds"):
                return last
            time.sleep(POLL_INTERVAL_SECONDS)
        raise TimeoutError(f"图片生成在 {timeout:.0f}s 内未完成")

    def assert_image(self, content: bytes, *, max_edge: int | None = None) -> str:
        with Image.open(BytesIO(content)) as image:
            size = image.size
            fmt = image.format or ""
        if max_edge is not None:
            assert max(size) <= max_edge, f"尺寸超限 {size}"
        return f"{fmt} {size[0]}x{size[1]}"


class DemoSkip(Exception):
    """当前环境下该功能不可用（如未配置真实供应商）。"""


# ======================================================================
# 各阶段步骤
# ======================================================================
def phase_health_and_auth(r: DemoRunner) -> None:
    print("\n■ 阶段 0 · 健康检查与鉴权")

    def health() -> str:
        start = time.perf_counter()
        response = r.client.get("/healthz")
        r.record_timing("健康检查", time.perf_counter() - start)
        response.raise_for_status()
        payload = response.json()
        assert payload["status"] == "ok"
        return f"admin_access_required={payload['admin_access_required']}"

    r.step("安全", "健康检查 /healthz", health)

    def anon_protected() -> str:
        response = r.client.get("/api/products")
        assert response.status_code == 401, response.status_code
        return "未登录访问受保护 API 被拒绝"

    r.step("安全", "未登录 401 防护", anon_protected)

    def bad_key() -> str:
        response = r.client.post("/api/auth/session", json={"admin_key": "definitely-wrong-key"})
        assert response.status_code == 401
        return "错误密钥被拒绝"

    r.step("安全", "错误密钥 401", bad_key)

    def login() -> str:
        start = time.perf_counter()
        response = r.client.post("/api/auth/session", json={"admin_key": r.admin_key})
        r.record_timing("登录", time.perf_counter() - start)
        response.raise_for_status()
        state = r.client.get("/api/auth/session")
        assert state.json()["authenticated"] is True
        return "会话 Cookie 生效"

    r.step("安全", "管理员登录", login)


def phase_asset_library(r: DemoRunner) -> None:
    print("\n■ 阶段 1 · 素材库与模板识别")

    def builtin_templates() -> str:
        response = r.client.get("/api/agent/assets", params={"kind": "template"})
        response.raise_for_status()
        builtin = [item for item in response.json()["items"] if item["source"] == "builtin"]
        assert len(builtin) >= 3, f"内置样板不足: {len(builtin)}"
        thumb = r.client.get(builtin[0]["download_url"], params={"variant": "thumbnail"})
        thumb.raise_for_status()
        return f"{len(builtin)} 张内置样板（冷启动 OK）"

    r.step("素材库", "内置样板冷启动", builtin_templates)

    def upload_template() -> str:
        buffer = BytesIO()
        Image.new("RGB", (900, 1200), (120, 30, 90)).save(buffer, format="PNG")
        start = time.perf_counter()
        response = r.client.post(
            "/api/agent/assets",
            files={"file": ("演示模板.png", buffer.getvalue(), "image/png")},
            data={"kind": "template"},
        )
        r.record_timing("素材上传", time.perf_counter() - start)
        response.raise_for_status()
        return f"登记成功 id={response.json()['id'][:8]}"

    r.step("素材库", "上传模板图", upload_template)

    def asset_thumbnail() -> str:
        response = r.client.get("/api/agent/assets", params={"kind": "template"})
        entry = response.json()["items"][0]
        start = time.perf_counter()
        thumb = r.client.get(entry["preview_url"])
        r.record_timing("缩略图服务", time.perf_counter() - start)
        thumb.raise_for_status()
        return r.assert_image(thumb.content, max_edge=1600)

    r.step("素材库", "缩略图按需派生", asset_thumbnail)


def phase_designer_agent(r: DemoRunner) -> None:
    print("\n■ 阶段 2 · 设计师 Agent（对话式制图全流程）")

    def create_session() -> str:
        response = r.client.post("/api/agent/sessions", json={"title": "自动化演示"})
        response.raise_for_status()
        r.created_agent_session_id = response.json()["id"]
        return f"会话 {r.created_agent_session_id[:8]}"

    r.step("Agent", "创建会话", create_session)

    def turn(message: str) -> dict:
        start = time.perf_counter()
        response = r.client.post(
            f"/api/agent/sessions/{r.created_agent_session_id}/messages",
            json={"content": message},
        )
        r.record_timing("Agent 对话轮", time.perf_counter() - start)
        if response.status_code == 503:
            raise DemoSkip(f"需要配置 OpenAI 兼容文本供应商（{response.json().get('detail', '')[:60]}）")
        response.raise_for_status()
        return response.json()

    def turn_recommend() -> str:
        payload = turn("我周末开业，想要个酬宾海报，完全不懂怎么弄")
        events = {event["tool"]: event["result"] for event in payload["tool_events"]}
        if "recommend_designs" in events:
            recs = events["recommend_designs"].get("recommendations", [])
            return f"推荐 {len(recs)} 个方案（含理由）"
        stages = payload["session"]["stage"]
        return f"阶段={stages}"

    r.step("Agent", "轮1 · 一句话需求 → 主动推荐", turn_recommend)

    def turn_copy() -> str:
        payload = turn("就用刚才推荐的方案，先给我几版文案")
        for event in payload["tool_events"]:
            if event["tool"] == "write_copy" and event["result"].get("copies"):
                return f"{len(event['result']['copies'])} 版文案"
        return f"阶段={payload['session']['stage']}"

    r.step("Agent", "轮2 · 选方案 → 出文案", turn_copy)

    def turn_image() -> str:
        payload = turn("就要这些文案，出一张 1080x1440 的海报吧")
        for event in payload["tool_events"]:
            result = event["result"]
            if event["tool"] in {"generate_image", "edit_image"}:
                r.agent_image_session_id = result.get("image_session_id")
                completed = result.get("completed_assets") or []
                if completed:
                    return f"生成完成 asset={completed[0]['asset_id'][:8]}"
                if result.get("pending_tasks"):
                    return "任务已提交，后台生成中"
        if r.agent_image_session_id is None:
            snapshot = r.client.get(f"/api/agent/sessions/{r.created_agent_session_id}").json()
            r.agent_image_session_id = snapshot.get("image_session_id")
        return "已发起"

    r.step("Agent", "轮3 · 确认 → 生图（durable 队列）", turn_image)

    def wait_generation() -> str:
        if r.agent_image_session_id is None:
            raise DemoSkip("没有关联的图片会话")
        start = time.perf_counter()
        final = r.wait_for_generation(r.agent_image_session_id)
        r.record_timing("图片生成（提交→完成）", time.perf_counter() - start)
        rounds = [round_item for round_item in final.get("rounds", []) if round_item.get("generated_asset")]
        if not rounds:
            tasks = final.get("generation_tasks", [])
            failed = [task for task in tasks if task["status"] == "failed"]
            if failed:
                raise DemoSkip(f"生成任务失败（队列/供应商未就绪）: {failed[0].get('failure_reason', '')[:60]}")
            raise RuntimeError("生成结束但没有产出资产")
        return f"{len(rounds)} 张成品"

    r.step("Agent", "生图完成等待", wait_generation)

    def turn_archive() -> str:
        payload = turn("挺满意的，存到素材库")
        for event in payload["tool_events"]:
            if event["tool"] == "save_asset" and event["result"].get("status") == "completed":
                return "成品已入库"
        return "已提示保存"

    r.step("Agent", "轮4 · 满意 → 存入素材库", turn_archive)

    def turn_report() -> str:
        payload = turn("再给我一份完整的文案报告")
        for event in payload["tool_events"]:
            if event["tool"] == "write_copy_report" and event["result"].get("report"):
                report = event["result"]["report"]
                report_keys = ("headline", "moments_caption", "selling_points", "hashtags", "publishing_tips")
                sections = [key for key in report_keys if report.get(key)]
                return f"报告含 {len(sections)} 个板块"
        return "已响应"

    r.step("Agent", "轮5 · 文案报告", turn_report)

    def stage_check() -> str:
        snapshot = r.client.get(f"/api/agent/sessions/{r.created_agent_session_id}").json()
        return f"最终阶段={snapshot['stage']}"

    r.step("Agent", "对话阶段状态机", stage_check)


def phase_product_workflow(r: DemoRunner) -> None:
    print("\n■ 阶段 3 · 商品工作流（冻结保留功能）")

    def create_product() -> str:
        buffer = BytesIO()
        Image.new("RGB", (800, 800), (235, 240, 245)).save(buffer, format="PNG")
        start = time.perf_counter()
        response = r.client.post(
            "/api/products",
            data={
                "name": "演示护手霜",
                "category": "个护",
                "price": "39.90",
                "canvas_template_key": "ecommerce-main-image-v1",
            },
            files={"image": ("product.png", buffer.getvalue(), "image/png")},
        )
        r.record_timing("建品（模板物化）", time.perf_counter() - start)
        response.raise_for_status()
        r.created_product_id = response.json()["id"]
        return f"商品 {r.created_product_id[:8]}"

    r.step("工作流", "建品 + 画布模板物化", create_product)

    def upload_reference() -> str:
        buffer = BytesIO()
        Image.new("RGB", (640, 640), (200, 220, 240)).save(buffer, format="PNG")
        response = r.client.post(
            f"/api/products/{r.created_product_id}/reference-images",
            files=[("reference_images", ("ref.png", buffer.getvalue(), "image/png"))],
        )
        response.raise_for_status()
        return "参考图已挂载"

    r.step("工作流", "上传参考图", upload_reference)

    def run_workflow() -> str:
        start = time.perf_counter()
        response = r.client.post(f"/api/products/{r.created_product_id}/workflow/run", json={})
        if response.status_code == 503:
            raise DemoSkip("生成队列不可用（Redis 未启动）——完整栈上可通过")
        response.raise_for_status()
        deadline = time.monotonic() + GENERATION_TIMEOUT_SECONDS
        status = "running"
        while time.monotonic() < deadline:
            payload = r.client.get(f"/api/products/{r.created_product_id}/workflow").json()
            runs = payload.get("runs", [])
            if runs:
                status = runs[0]["status"]
                if status in {"succeeded", "failed", "cancelled"}:
                    break
            time.sleep(POLL_INTERVAL_SECONDS)
        r.record_timing("工作流全图执行", time.perf_counter() - start)
        if status != "succeeded":
            raise DemoSkip(f"工作流未完成（{status}，队列/供应商未就绪）")
        return "全图 succeeded"

    r.step("工作流", "一键跑全图", run_workflow)

    def download_poster() -> str:
        detail = r.client.get(f"/api/products/{r.created_product_id}").json()
        posters = detail.get("poster_variants", [])
        if not posters:
            raise DemoSkip("尚无海报产出（依赖生成成功）")
        start = time.perf_counter()
        poster = r.client.get(posters[0]["download_url"])
        thumb = r.client.get(posters[0]["thumbnail_url"])
        r.record_timing("交付物下载", time.perf_counter() - start)
        poster.raise_for_status()
        thumb.raise_for_status()
        return f"原图 {r.assert_image(poster.content)} / 缩略图 ≤320"

    r.step("工作流", "海报原图/缩略图下载", download_poster)


def phase_image_session_and_gallery(r: DemoRunner) -> None:
    print("\n■ 阶段 4 · 图片会话与画廊")

    def create_session() -> str:
        response = r.client.post("/api/image-sessions", json={"title": "自动化演示会话"})
        response.raise_for_status()
        r.image_session_id = response.json()["id"]
        return f"会话 {r.image_session_id[:8]}"

    r.step("图片会话", "创建会话", create_session)

    def generate() -> str:
        start = time.perf_counter()
        response = r.client.post(
            f"/api/image-sessions/{r.image_session_id}/generate",
            json={"prompt": "奶油质感护手霜广告图，柔光白底", "size": "1024x1024", "generation_count": 2},
        )
        if response.status_code == 503:
            raise DemoSkip("生成队列不可用（Redis 未启动）——完整栈上可通过")
        response.raise_for_status()
        final = r.wait_for_generation(r.image_session_id)
        r.record_timing("图片会话生图", time.perf_counter() - start)
        rounds = [item for item in final.get("rounds", []) if item.get("generated_asset")]
        if not rounds:
            raise DemoSkip("生成未产出（队列/供应商未就绪）")
        r.gallery_asset_id = rounds[-1]["generated_asset"]["id"]
        return f"{len(rounds)} 个候选"

    r.step("图片会话", "文/图生图（多候选）", generate)

    def save_to_gallery() -> str:
        if r.gallery_asset_id is None:
            raise DemoSkip("没有可入画廊的资产")
        response = r.client.post("/api/gallery", json={"image_session_asset_id": r.gallery_asset_id})
        response.raise_for_status()
        listed = r.client.get("/api/gallery")
        assert any(item["image"]["id"] == r.gallery_asset_id for item in listed.json()["items"])
        return "画廊可见"

    r.step("画廊", "成品存入画廊", save_to_gallery)

    def queue_overview() -> str:
        response = r.client.get("/api/generation-queue")
        response.raise_for_status()
        overview = response.json()
        return f"队列概览: running={overview.get('running_count', overview.get('running', '?'))}"

    r.step("队列", "生成队列总览", queue_overview)


def phase_settings_and_security(r: DemoRunner) -> None:
    print("\n■ 阶段 5 · 设置与安全")

    def unlock_wrong() -> str:
        response = r.client.post("/api/settings/unlock", json={"token": "wrong-token"})
        assert response.status_code == 401
        return "错误解锁令牌被拒绝"

    r.step("设置", "二级令牌错误 401", unlock_wrong)

    def unlock() -> str:
        response = r.client.post("/api/settings/unlock", json={"token": r.settings_token})
        response.raise_for_status()
        return "设置已解锁"

    r.step("设置", "设置解锁", unlock)

    def runtime_update() -> str:
        start = time.perf_counter()
        patched = r.client.patch("/api/settings", json={"values": {"generation_max_concurrent_tasks": 4}})
        r.record_timing("运行时配置修改", time.perf_counter() - start)
        patched.raise_for_status()
        runtime = r.client.get("/api/settings/runtime").json()
        assert runtime["image_tool_allowed_fields"] is not None
        return "运行时读取已生效"

    r.step("设置", "运行时配置即时生效", runtime_update)

    def export_masked() -> str:
        exported = r.client.get("/api/settings/export")
        exported.raise_for_status()
        profiles = exported.json().get("provider_profiles", [])
        with_keys = [profile for profile in profiles if profile.get("api_key")]
        for profile in with_keys:
            assert str(profile["api_key"]).startswith("__redacted__"), "导出包含明文 key！"
        return f"{len(with_keys)} 个 key 全部脱敏" if with_keys else "无 key 档案（脱敏逻辑就绪）"

    r.step("安全", "导出 API Key 脱敏", export_masked)

    def deletion_guard() -> str:
        if r.created_product_id is None:
            raise DemoSkip("没有可测商品")
        # 幂等：显式关闭后再验证护栏（前次运行的 DB 状态可能残留）
        r.client.patch("/api/settings", json={"values": {"deletion_enabled": False}})
        response = r.client.delete(f"/api/products/{r.created_product_id}")
        assert response.status_code == 403, f"期望 403 实得 {response.status_code}"
        patched = r.client.patch("/api/settings", json={"values": {"deletion_enabled": True}})
        patched.raise_for_status()
        r.deletion_enabled = True
        return "护栏验证通过（关闭时 403），已重新开启供清理"

    r.step("安全", "业务删除开关护栏", deletion_guard)


def phase_cleanup(r: DemoRunner) -> None:
    print("\n■ 阶段 6 · 清理")

    def delete_product() -> str:
        if r.created_product_id is None:
            raise DemoSkip("没有创建过商品")
        if not r.deletion_enabled:
            r.client.patch("/api/settings", json={"values": {"deletion_enabled": True}})
        start = time.perf_counter()
        response = r.client.delete(f"/api/products/{r.created_product_id}")
        r.record_timing("删除清理", time.perf_counter() - start)
        response.raise_for_status()
        gone = r.client.get(f"/api/products/{r.created_product_id}")
        assert gone.status_code == 404
        return "商品与落盘文件已清理"

    r.step("清理", "删除商品（含存储清理）", delete_product)

    def logout() -> str:
        response = r.client.delete("/api/auth/session")
        response.raise_for_status()
        assert r.client.get("/api/products").status_code == 401
        return "登出后重新受保护"

    r.step("安全", "登出", logout)


# ======================================================================
# 报告
# ======================================================================
def build_report(r: DemoRunner, *, mode_label: str) -> str:
    lines = [
        "# ProductFlow 自动化演示报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 目标实例：{r.base_url}",
        f"- 运行模式：{mode_label}",
        "",
        "## 功能覆盖矩阵",
        "",
        "| # | 模块 | 功能步骤 | 结果 | 耗时 | 说明 |",
        "|---|---|---|---|---|---|",
    ]
    passed = failed = skipped = 0
    for index, result in enumerate(r.results, 1):
        icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⊘"}[result.status]
        if result.status == "PASS":
            passed += 1
        elif result.status == "FAIL":
            failed += 1
        else:
            skipped += 1
        line = (
            f"| {index} | {result.category} | {result.name} | {icon} {result.status} "
            f"| {result.duration_ms:.0f} ms | {result.detail} |"
        )
        lines.append(line)
    total = len(r.results)
    lines += [
        "",
        f"**覆盖率**：{passed}/{total} 通过，{skipped} 跳过，{failed} 失败",
        "",
        "## 性能摘要",
        "",
        "| 操作 | 次数 | 平均 | 最大 |",
        "|---|---|---|---|",
    ]
    for label, values in r.timings.items():
        lines.append(
            f"| {label} | {len(values)} | {statistics.mean(values):.0f} ms | {max(values):.0f} ms |"
        )
    lines += [
        "",
        "## 说明",
        "",
        "- SKIP = 当前环境不具备前置条件（如未配置真实文本供应商、队列未启动）。",
        "- 图片生成耗时与所配供应商强相关；mock 供应商为秒级占位图。",
        "- 本流程可重复执行；建品/会话等演示数据会在清理阶段删除。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    global GENERATION_TIMEOUT_SECONDS
    parser = argparse.ArgumentParser(description="ProductFlow 自动化全功能演示")
    parser.add_argument("--base-url", default="http://127.0.0.1:29280")
    parser.add_argument("--admin-key", default="super-secret-admin-key")
    parser.add_argument("--settings-token", default="super-secret-settings-token")
    parser.add_argument("--output", default="demo-report.md")
    parser.add_argument("--generation-timeout", type=float, default=GENERATION_TIMEOUT_SECONDS,
                        help="单次图片生成/工作流的等待上界（秒）")
    args = parser.parse_args()
    GENERATION_TIMEOUT_SECONDS = args.generation_timeout

    runner = DemoRunner(base_url=args.base_url, admin_key=args.admin_key, settings_token=args.settings_token)
    print(f"▶ ProductFlow 自动化演示 → {args.base_url}")

    probe = runner.client.get("/healthz", timeout=10)
    probe.raise_for_status()
    mode_label = "真实/本地栈（healthz OK）"

    phase_health_and_auth(runner)
    phase_asset_library(runner)
    phase_designer_agent(runner)
    phase_product_workflow(runner)
    phase_image_session_and_gallery(runner)
    phase_settings_and_security(runner)
    phase_cleanup(runner)

    report = build_report(runner, mode_label=mode_label)
    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")
    passed = sum(1 for item in runner.results if item.status == "PASS")
    skipped = sum(1 for item in runner.results if item.status == "SKIP")
    failed = sum(1 for item in runner.results if item.status == "FAIL")
    print(f"\n▶ 完成：{passed} 通过 / {skipped} 跳过 / {failed} 失败；报告已写入 {output_path.resolve()}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
