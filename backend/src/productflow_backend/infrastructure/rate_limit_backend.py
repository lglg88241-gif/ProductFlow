"""限速计数存储后端：进程内存实现与 Redis 原子实现（审计 A1）。

历史缺陷（审计 A1）：限速状态存进程内存——多进程/多实例部署不共享计数、重启即清零，
爆破防护可被横向绕过。本模块把"固定窗口计数"抽成 :class:`RateLimitStore` 协议：

- :class:`InMemoryRateLimitStore`：现有语义的内存实现（单实例/测试；Redis 不可用时的显式降级）；
- :class:`RedisRateLimitStore`：redis-py 同步客户端实现，键统一加 ``pf:rl:`` 前缀；
  INCR 原子递增，TTL 只在首次写入时设置（不随命中刷新，否则固定窗口会被永久续期）。

多实例共享同一 Redis 后，登录/解锁爆破防护在进程间生效；键有 TTL，无界增长由过期解决。
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

import redis

# 限速状态最多跟踪的键数量（memory 后端）：超出后优先淘汰已过期键，再按最早到期淘汰，
# 保证内存有界。Redis 后端无此参数——无界增长由 TTL 过期解决。
DEFAULT_MAX_TRACKED_KEYS = 4096


class RateLimitStore(Protocol):
    """固定窗口计数存储的最小接口。

    incr 必须原子：递增并在首次写入时设置过期（TTL 到期计数归零，窗口重新开始）。
    delete/reset_all 供成功清零与测试复位使用；key_count 是两个内置实现附带的
    测试辅助扩展，自定义替身可以不实现。
    """

    def incr(self, key: str, window_seconds: int) -> int:
        """原子递增并返回新计数；首次写入时为键设置 window_seconds 过期。"""
        ...

    def get(self, key: str) -> int:
        """当前计数；键不存在或已过期返回 0。"""
        ...

    def delete(self, key: str) -> None:
        """删除单个键（登录/解锁成功后清零）。"""
        ...

    def reset_all(self, prefix: str) -> None:
        """删除所有以 prefix 开头的键（测试复位用）。"""
        ...


class InMemoryRateLimitStore:
    """进程内存版固定窗口计数（单实例语义，线程安全）。

    窗口语义与 Redis 版对齐：计数自首次写入起算，window_seconds 后整体过期归零。
    max_keys 上限淘汰过期键与最早到期键，防止伪造大量身份撑爆内存。
    """

    def __init__(self, *, max_keys: int = DEFAULT_MAX_TRACKED_KEYS) -> None:
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        # key -> (count, expires_at monotonic)
        self._counts: dict[str, tuple[int, float]] = {}

    def incr(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        with self._lock:
            entry = self._counts.get(key)
            if entry is None or entry[1] <= now:
                count, expires_at = 1, now + window_seconds
            else:
                count, expires_at = entry[0] + 1, entry[1]
            if key not in self._counts:
                self._evict_locked(now)
            self._counts[key] = (count, expires_at)
            return count

    def get(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            entry = self._counts.get(key)
            if entry is None:
                return 0
            if entry[1] <= now:
                self._counts.pop(key, None)
                return 0
            return entry[0]

    def delete(self, key: str) -> None:
        with self._lock:
            self._counts.pop(key, None)

    def reset_all(self, prefix: str) -> None:
        with self._lock:
            for key in [key for key in self._counts if key.startswith(prefix)]:
                self._counts.pop(key, None)

    def key_count(self, prefix: str) -> int:
        """当前存活的、以 prefix 开头的键数量（容量守卫与测试断言用）。"""
        now = time.monotonic()
        with self._lock:
            return sum(
                1
                for key, (_, expires_at) in self._counts.items()
                if key.startswith(prefix) and expires_at > now
            )

    def _evict_locked(self, now: float) -> None:
        """内存必须有界：先淘汰已过期键，仍满则淘汰最早到期的键（调用方持锁）。"""
        if len(self._counts) < self._max_keys:
            return
        for key in [key for key, (_, expires_at) in self._counts.items() if expires_at <= now]:
            del self._counts[key]
        while len(self._counts) >= self._max_keys:
            oldest = min(self._counts, key=lambda key: self._counts[key][1])
            del self._counts[oldest]


class RedisRateLimitStore:
    """Redis 原子固定窗口计数（多进程/多实例共享）。

    - 键统一加 ``pf:rl:`` 前缀，与 Dramatiq 队列键空间隔离；
    - INCR 原子递增；TTL 只在首次写入（或 TTL 丢失自愈）时设置，命中不刷新——
      否则持续爆破会把窗口永久续期，限速退化为"每窗口 5 次且窗口永不重置"；
    - 连接串复用 get_settings().redis_url（由调用方传入），与本进程 Dramatiq broker 同源。
    """

    def __init__(
        self,
        redis_url: str,
        *,
        prefix: str = "pf:rl:",
        socket_connect_timeout: float = 2.0,
        socket_timeout: float = 2.0,
    ) -> None:
        self._prefix = prefix
        self._client = redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=socket_connect_timeout,
            socket_timeout=socket_timeout,
        )

    def ping(self) -> None:
        """连通性探测（后端选择 auto 模式使用；失败抛 redis 异常）。"""
        self._client.ping()

    def _full_key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def incr(self, key: str, window_seconds: int) -> int:
        full = self._full_key(key)
        # pipeline(transaction=True) = MULTI/EXEC：INCR+TTL 原子读取，避免中间态
        pipe = self._client.pipeline(transaction=True)
        pipe.incr(full)
        pipe.ttl(full)
        count, ttl = pipe.execute()
        if ttl is None or int(ttl) < 0:
            # 首次写入（ttl=-1 无过期）才设置 TTL；-2 理论不可达（INCR 后键必存在），一并自愈
            self._client.expire(full, window_seconds)
        return int(count)

    def get(self, key: str) -> int:
        value = self._client.get(self._full_key(key))
        return int(value) if value is not None else 0

    def delete(self, key: str) -> None:
        self._client.delete(self._full_key(key))

    def reset_all(self, prefix: str) -> None:
        pattern = f"{self._full_key(prefix)}*"
        batch: list[str] = []
        for key in self._client.scan_iter(match=pattern, count=500):
            batch.append(str(key))
            if len(batch) >= 500:
                self._client.delete(*batch)
                batch.clear()
        if batch:
            self._client.delete(*batch)

    def key_count(self, prefix: str) -> int:
        pattern = f"{self._full_key(prefix)}*"
        return sum(1 for _ in self._client.scan_iter(match=pattern, count=500))
