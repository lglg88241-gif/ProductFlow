"""进程内失败限速器：滑动窗口内失败达到上限即拒绝，成功后清零。

单实例部署的内存级实现（多实例部署需换 Redis 计数）。
"""

from __future__ import annotations

import math
import threading
import time

from fastapi import HTTPException, Request, status


def client_ip(request: Request) -> str:
    """取真实客户端 IP。

    部署约定：外部流量经 web 容器 nginx 同源反代（backend 只绑 127.0.0.1），
    此时 request.client.host 恒为 nginx IP，限速会退化为全局桶——因此优先取
    X-Forwarded-For 第一跳（原始客户端）。直连绕过反代伪造该头的风险面
    仅限本机，可接受。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


class FailureRateLimiter:
    """按 key（通常是客户端 IP）记录失败次数，窗口内达到上限抛 429。"""

    def __init__(self, *, limit: int, window_seconds: int, too_many_message: str) -> None:
        self._limit = limit
        self._window = window_seconds
        self._message = too_many_message
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}

    def _prune(self, now: float) -> None:
        expired_before = now - self._window
        for key in list(self._failures):
            recent = [ts for ts in self._failures[key] if ts > expired_before]
            if recent:
                self._failures[key] = recent
            else:
                self._failures.pop(key, None)

    def check(self, key: str) -> None:
        """未达上限直接返回；达到上限抛 429（带 Retry-After）。"""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
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

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)

    def reset_all(self) -> None:
        """测试专用：清空全部失败记录。"""
        with self._lock:
            self._failures.clear()


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
