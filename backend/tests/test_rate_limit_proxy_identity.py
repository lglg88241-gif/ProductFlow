"""代理身份与限速资源边界（审计 S1／M1／M2）。

复现的两个问题：
1. nginx 使用 `$proxy_add_x_forwarded_for` 追加客户端自报的 XFF，后端又取第一项
   → 客户端可自带 `X-Forwarded-For: 1.2.3.4` 就伪造身份，限速被绕过。
2. 限速器每次 check 都持锁遍历全部 key，且 key 无上限 → 伪造大量不同身份即可
   把内存撑成无界字典，同时每次请求做 O(n) 扫描。

本文件与修复一一对应，可独立回退。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "web" / "nginx.conf"


def _make_request(*, peer: str, xff: str | None = None):
    """构造一个只带 client/headers 的最小请求替身。"""

    class _Client:
        host = peer

    class _Request:
        client = _Client()

        def __init__(self) -> None:
            self.headers = {"x-forwarded-for": xff} if xff else {}

        def __getitem__(self, name: str) -> str:
            return self.headers[name]

    return _Request()


def test_client_supplied_xff_is_ignored_from_untrusted_peer(configured_env: Path) -> None:
    """不可信来源自报的 XFF 必须被忽略——否则可伪造身份绕过限速。"""
    from productflow_backend.presentation.rate_limit import client_ip

    request = _make_request(peer="203.0.113.9", xff="1.2.3.4")
    assert client_ip(request) == "203.0.113.9", "不可信来源的 XFF 被采信了"


def test_trusted_proxy_xff_is_honoured(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有来自显式可信代理的连接才解析 XFF（单层 nginx 会覆盖为真实来源）。"""
    from productflow_backend.presentation.rate_limit import client_ip

    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.18.0.0/16")
    request = _make_request(peer="172.18.0.5", xff="198.51.100.7")
    assert client_ip(request) == "198.51.100.7"


def test_invalid_forwarded_value_falls_back_to_peer(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XFF 内容非法时必须回落到实际对端，不能把任意字符串当身份。"""
    from productflow_backend.presentation.rate_limit import client_ip

    monkeypatch.setenv("TRUSTED_PROXY_IPS", "172.18.0.0/16")
    request = _make_request(peer="172.18.0.5", xff="not-an-ip, 198.51.100.7")
    assert client_ip(request) == "198.51.100.7", "非法项应被跳过"


def test_untrusted_peer_with_garbage_xff_still_uses_peer(configured_env: Path) -> None:
    from productflow_backend.presentation.rate_limit import client_ip

    request = _make_request(peer="203.0.113.9", xff="<script>alert(1)</script>")
    assert client_ip(request) == "203.0.113.9"


def test_rate_limiter_key_count_is_bounded(configured_env: Path) -> None:
    """大量不同身份不得把限速器撑成无界字典（内存 DoS 防护）。"""
    from productflow_backend.presentation.rate_limit import FailureRateLimiter

    limiter = FailureRateLimiter(limit=5, window_seconds=60, too_many_message="x", max_keys=100)
    for index in range(500):
        limiter.record(f"10.0.{index // 250}.{index % 250}")
    assert limiter.tracked_key_count() <= 100, "限速键数量必须有上界"


def test_nginx_overwrites_client_supplied_forwarded_header() -> None:
    """静态门禁：nginx 不得用 $proxy_add_x_forwarded_for（追加会被伪造）。"""
    text = NGINX_CONF.read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in text.splitlines()
        if "X-Forwarded-For" in line and "$proxy_add_x_forwarded_for" in line
    ]
    assert not offenders, (
        "nginx 追加了客户端自报的 X-Forwarded-For，客户端可借此伪造来源 IP："
        f"{offenders}。单层入口应覆盖为 $remote_addr。"
    )
    assert "$remote_addr" in text
