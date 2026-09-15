"""进程内失败限速器：滑动窗口内失败达到上限即拒绝，成功后清零。

单实例部署的内存级实现（多实例部署需换 Redis 计数，见审计 A1 未完成项）。
"""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time

from fastapi import HTTPException, Request, status

# 限速状态最多跟踪的身份数量：超出后按最早失败时间淘汰，保证内存有界
DEFAULT_MAX_TRACKED_KEYS = 4096
# 全量清理的最小间隔（秒）：避免每次请求都持锁 O(n) 扫描
_PRUNE_MIN_INTERVAL_SECONDS = 5.0


def _trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """可信代理名单（TRUSTED_PROXY_IPS，逗号分隔的 IP 或 CIDR）。

    默认为空 = 不信任任何来源的 X-Forwarded-For。本地 compose 里 web 容器在
    172.16.0.0/12 网段，需要显式配置才解析 XFF——这样"忘记配置"退化为
    "忽略 XFF"（安全），而不是"信任任何人"（危险）。
    """
    raw = os.getenv("TRUSTED_PROXY_IPS", "").strip()
    networks = []
    for item in raw.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return networks


def _normalize_ip(value: str | None) -> str | None:
    """把输入规范化为合法 IP 字符串；非法输入返回 None。"""
    if not value:
        return None
    candidate = value.strip().strip("[]")
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _peer_is_trusted_proxy(peer_ip: str) -> bool:
    try:
        address = ipaddress.ip_address(peer_ip)
    except ValueError:
        return False
    return any(address in network for network in _trusted_proxy_networks())


def client_ip(request: Request) -> str:
    """取用于限速的身份：默认只认实际 TCP 对端。

    历史缺陷（审计 S1）：直接取 X-Forwarded-For 第一项，而 nginx 用
    $proxy_add_x_forwarded_for 追加客户端自报值——任何人发一个
    `X-Forwarded-For: 1.2.3.4` 就能伪造身份，同时把限速字典撑成无界内存。

    现在：不可信来源的 XFF 一律忽略；只有来自显式可信代理的连接才解析 XFF，
    且逐项做 IP 合法性校验，非法项跳过。
    """
    peer = _normalize_ip(request.client.host if request.client else None) or "unknown"
    if not _peer_is_trusted_proxy(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    for part in forwarded.split(","):
        normalized = _normalize_ip(part)
        if normalized:
            return normalized
    return peer


class FailureRateLimiter:
    """按 key（通常是客户端 IP）记录失败次数，窗口内达到上限抛 429。"""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        too_many_message: str,
        max_keys: int = DEFAULT_MAX_TRACKED_KEYS,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._message = too_many_message
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._last_prune = 0.0

    def _prune(self, now: float) -> None:
        expired_before = now - self._window
        for key in list(self._failures):
            recent = [ts for ts in self._failures[key] if ts > expired_before]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)

    def tracked_key_count(self) -> int:
        """当前跟踪的身份数量（供容量守卫与测试断言）。"""
        with self._lock:
            return len(self._failures)

    def _evict_oldest(self, now: float) -> None:
        """超出容量时淘汰最早失败的一批身份——内存必须有界，调用方持锁。"""
        if len(self._failures) <= self._max_keys:
            return
        ordered = sorted(self._failures.items(), key=lambda item: item[1][0] if item[1] else 0.0)
        overflow = len(ordered) - self._max_keys
        for key, _ in ordered[: max(1, overflow)]:
            self._failures.pop(key, None)

    def check(self, key: str) -> None:
        """未达上限直接返回；达到上限抛 429（带 Retry-After）。"""
        now = time.monotonic()
        with self._lock:
            # 限制全量清理频率：并发请求下不再每次持锁 O(n) 扫描
            if now - self._last_prune >= _PRUNE_MIN_INTERVAL_SECONDS:
                self._prune(now)
                self._last_prune = now
            self._evict_oldest(now)
            recent = self._failures.get(key, [])
            if len(recent) < self._limit:
                return
            retry_after = max(1, math.ceil(recent[0] + self._window - now))
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=self._message,
            headers={"Retry-After": str(retry_after)},
        )

    def record(self, key: str) -> None:
        with self._lock:
            self._failures.setdefault(key, []).append(time.monotonic())
            self._evict_oldest(time.monotonic())

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset_all(self) -> None:
        """测试专用：清空全部失败记录。"""
        with self._lock:
            self._failures.clear()
            self._last_prune = 0.0


_LOGIN_WINDOW_SECONDS = 15 * 60

# 管理员登录：15 分钟窗口内第 6 次失败起 429
login_rate_limiter = FailureRateLimiter(
    limit=5,
    window_seconds=_LOGIN_WINDOW_SECONDS,
    too_many_message="登录失败次数过多，请 15 分钟后再试",
)

# 设置页二次令牌解锁：与登录同一策略（此前可无限爆破）
settings_unlock_rate_limiter = FailureRateLimiter(
    limit=5,
    window_seconds=_LOGIN_WINDOW_SECONDS,
    too_many_message="设置解锁尝试过多，请 15 分钟后再试",
)
