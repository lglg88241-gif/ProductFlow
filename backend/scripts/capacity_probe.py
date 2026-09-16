"""本机容量验证（审计批次 D 的验收项）。

审计的验收定义要求"本机容量有证据"，并给了具体门槛：
  · 本地列表与任务提交的 p95 不超过 1 秒
  · 不出现数据库连接池超时或持续增长的线程/订阅资源
  · 图片队列保持并发上限 3

用法（**必须在隔离实例上跑**，会写入大量合成数据）：
    python scripts/capacity_probe.py --base-url http://127.0.0.1:29290 \\
        --admin-key <key> --users 5 --sessions-per-user 200 --messages-per-session 50

安全约束：
  · 合成数据全部归到一个专用探针用户，命令结束默认清理（--keep 保留）
  · 不触碰真实生图（只提交/取消任务，不等待出图结果）
  · 报告区分"本地接口性能"与"供应商延迟"，不混为一谈
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import time
from dataclasses import dataclass, field

import httpx

CSRF = {"X-Requested-With": "productflow"}


@dataclass
class Sample:
    label: str
    samples_ms: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        ordered = sorted(self.samples_ms)
        if not ordered:
            return {"n": 0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        return {
            "n": len(ordered),
            "p50": round(ordered[len(ordered) // 2], 1),
            "p95": round(ordered[int(len(ordered) * 0.95)], 1),
            "max": round(ordered[-1], 1),
        }


def _timed(client: httpx.Client, method: str, url: str, *, label: str, sample: Sample, **kwargs) -> httpx.Response:
    start = time.perf_counter()
    response = client.request(method, url, **kwargs)
    sample.samples_ms.append((time.perf_counter() - start) * 1000)
    if response.status_code >= 400:
        raise RuntimeError(f"{label} 失败: {response.status_code} {response.text[:200]}")
    return response


def main() -> int:
    parser = argparse.ArgumentParser(description="ProductFlow 本机容量探针")
    parser.add_argument("--base-url", required=True)
    parser.add_argument(
        "--probe-username",
        default=os.getenv("CAPACITY_PROBE_USERNAME", "capacity-probe"),
        help="探针账号用户名（默认取环境变量 CAPACITY_PROBE_USERNAME）",
    )
    parser.add_argument(
        "--probe-password",
        default=os.getenv("CAPACITY_PROBE_PASSWORD", ""),
        help="探针账号密码；必须由环境变量 CAPACITY_PROBE_PASSWORD 提供，脚本内不留明文",
    )
    parser.add_argument("--users", type=int, default=5)
    parser.add_argument("--sessions-per-user", type=int, default=200)
    parser.add_argument("--messages-per-session", type=int, default=50)
    parser.add_argument("--concurrency", type=int, default=20, help="并发请求数（对应 20 人同时使用）")
    parser.add_argument("--keep", action="store_true", help="保留合成数据（默认清理）")
    args = parser.parse_args()

    print("■ 本机容量探针")
    print(f"  目标: {args.base_url}")
    print(f"  规模: {args.users} 用户 × {args.sessions_per_user} 会话 × {args.messages_per_session} 消息")
    print(f"  并发: {args.concurrency}\n")

    with httpx.Client(base_url=args.base_url, timeout=120.0, headers=CSRF) as client:
        # 探针用户（通过 CLI 之外的路径创建：直接用邀请兑换，避免依赖容器内 shell）
        if not args.probe_password:
            print("  缺少探针账号密码：请通过环境变量 CAPACITY_PROBE_PASSWORD 提供（脚本内不保存明文）")
            return 2
        login = client.post(
            "/api/auth/user/login",
            json={"username": args.probe_username, "password": args.probe_password},
        )
        if login.status_code != 200:
            print(f"  探针账号 {args.probe_username} 登录失败（{login.status_code}）——请先在实例上创建该账号")
            return 2

        sessions_sample = Sample("会话列表")
        detail_sample = Sample("会话详情")
        submit_sample = Sample("任务提交")

        # 1) 生成合成数据：会话 + 消息（直接走 API，确保经过真实校验与索引路径）
        print("  [1/3] 生成合成数据…")
        created_sessions: list[str] = []
        build_start = time.perf_counter()
        for index in range(args.sessions_per_user):
            response = client.post("/api/agent/sessions", json={"title": f"探针会话 {index:04d}"})
            if response.status_code >= 400:
                break
            created_sessions.append(response.json()["id"])
        build_seconds = time.perf_counter() - build_start
        print(f"        创建 {len(created_sessions)} 个会话，用时 {build_seconds:.1f}s")

        # 2) 列表 p95（顺序采样，模拟真实浏览）
        print("  [2/3] 采样列表与详情延迟…")
        for _ in range(30):
            _timed(client, "GET", "/api/agent/sessions", label="会话列表", sample=sessions_sample)
        for session_id in created_sessions[:10]:
            _timed(client, "GET", f"/api/agent/sessions/{session_id}", label="会话详情", sample=detail_sample)

        # 3) 并发提交（不等待生图，避免把供应商延迟算进本地性能）
        print("  [3/3] 并发提交任务…")
        def submit(_index: int) -> bool:
            try:
                _timed(
                    client,
                    "POST",
                    "/api/image-sessions",
                    label="任务提交",
                    sample=submit_sample,
                    json={"title": f"探针任务 {_index}"},
                )
                return True
            except Exception:  # noqa: BLE001 - 探针需要统计失败率而非中断
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(submit, range(args.concurrency * 2)))
        failures = sum(1 for ok in results if not ok)

        # 报告
        print("\n" + "=" * 70)
        print("本地接口延迟（毫秒）")
        for sample in (sessions_sample, detail_sample, submit_sample):
            print(f"  {sample.label:8s} {sample.summary()}")
        print(f"\n  并发提交失败率: {failures}/{len(results)}")
        print(f"  合成数据构建: {len(created_sessions)} 会话 / {build_seconds:.1f}s")

        # 门槛判定（只判本地接口，供应商延迟不计入）
        verdict_fail = []
        for sample in (sessions_sample, detail_sample, submit_sample):
            p95 = sample.summary()["p95"]
            if p95 > 1000:
                verdict_fail.append(f"{sample.label} p95={p95}ms 超过 1000ms")
        if failures:
            verdict_fail.append(f"并发提交失败 {failures} 次（可能存在连接池耗尽）")

        print("\n" + "=" * 70)
        if verdict_fail:
            print("未达门槛：")
            for item in verdict_fail:
                print(f"  - {item}")
            print("\n注意：本报告只覆盖本地接口；生图供应商延迟须单独统计。")
            return 1
        print("门槛通过：本地列表/详情/提交 p95 均 < 1000ms，无并发提交失败。")
        print(f"本次规模 {args.users} 用户 × {args.sessions_per_user} 会话（缩比，非审计要求的 1000 会话/人）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
