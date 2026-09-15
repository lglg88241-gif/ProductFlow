"""真实 Redis 集成测试（审计 A1）。

运行条件：环境变量 ``RATE_LIMIT_REDIS_TEST_URL`` 指向一个可用的测试 Redis
（必须使用独立 DB，不碰 Dramatiq 队列的 DB 0；本机验证用 redis://127.0.0.1:16379/15，
Redis 7.4，真实跑通）。

跳过条件：该变量未设置，或连接失败 → pytest.skip。CI 无此变量时自动跳过；
本地验收时显式设置后真实运行，例如：
    RATE_LIMIT_REDIS_TEST_URL=redis://127.0.0.1:16379/15 python -m pytest tests/test_rate_limit_redis_integration.py -q

清理：测试结束删除本前缀（pf:rl:）下的全部键。
"""

from __future__ import annotations

import os
import threading
import time

import pytest
from fastapi import HTTPException

from productflow_backend.infrastructure.rate_limit_backend import RedisRateLimitStore
from productflow_backend.presentation.rate_limit import EntryRateLimiter, FailureRateLimiter

KEY_PREFIX = "pf:rl:"


def _redis_url() -> str:
    url = (os.getenv("RATE_LIMIT_REDIS_TEST_URL") or "").strip()
    if not url:
        pytest.skip("RATE_LIMIT_REDIS_TEST_URL 未设置：跳过真实 Redis 集成测试（CI 默认跳过）")
    return url


@pytest.fixture()
def redis_store() -> RedisRateLimitStore:
    store = RedisRateLimitStore(_redis_url())
    try:
        store.ping()
    except Exception as exc:  # 连不上同样跳过，不让集成测试冒充单元测试失败
        pytest.skip(f"测试 Redis 不可用（{type(exc).__name__}: {exc}）：跳过真实 Redis 集成测试")
    try:
        yield store
    finally:
        store.reset_all("")  # 清理本前缀全部键（专用测试 DB，不碰队列 DB 0）


def test_real_redis_incr_sets_ttl_once_and_expires(redis_store: RedisRateLimitStore) -> None:
    """INCR 原子递增 + 首次写入设 TTL；命中不续期；到期归零。"""
    key = "it:ttl"
    assert redis_store.incr(key, 2) == 1
    ttl_after_first = redis_store._client.ttl(KEY_PREFIX + key)
    assert 0 < ttl_after_first <= 2, "首次写入必须设置过期"
    assert redis_store.incr(key, 2) == 2
    ttl_after_second = redis_store._client.ttl(KEY_PREFIX + key)
    assert ttl_after_second <= ttl_after_first, "命中刷新了 TTL，固定窗口会被永久续期"
    time.sleep(2.05)
    assert redis_store.get(key) == 0, "TTL 到期后计数必须归零（无界增长由过期解决）"


def test_real_redis_incr_is_atomic_across_threads(redis_store: RedisRateLimitStore) -> None:
    workers, per_worker = 10, 20
    barrier = threading.Barrier(workers)

    def worker() -> None:
        barrier.wait(timeout=10)
        for _ in range(per_worker):
            redis_store.incr("it:atomic", 60)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert redis_store.get("it:atomic") == workers * per_worker, "INCR 并发不丢计数"


def test_real_redis_counters_shared_across_store_instances(redis_store: RedisRateLimitStore) -> None:
    """多进程/多实例共享语义：两个独立客户端连接看到同一计数。"""
    second = RedisRateLimitStore(_redis_url())
    assert redis_store.incr("it:shared", 60) == 1
    assert second.incr("it:shared", 60) == 2
    assert redis_store.get("it:shared") == 2


def test_real_redis_reset_all_removes_only_prefix(redis_store: RedisRateLimitStore) -> None:
    redis_store.incr("it:cleanup:a", 60)
    redis_store.incr("other:b", 60)
    redis_store.reset_all("it:")
    assert redis_store.get("it:cleanup:a") == 0
    assert redis_store.get("other:b") == 1


def test_real_redis_failure_limiter_blocks_and_clears(redis_store: RedisRateLimitStore) -> None:
    """走真实 Redis 的完整爆破防护时序：达限 429 → 成功清零放行。"""
    limiter = FailureRateLimiter(limit=3, window_seconds=60, too_many_message="太多", store=redis_store)
    for _ in range(3):
        limiter.check("203.0.113.1")
        limiter.record("203.0.113.1")
    with pytest.raises(HTTPException) as exc:
        limiter.check("203.0.113.1")
    assert exc.value.status_code == 429
    limiter.clear("203.0.113.1")
    limiter.check("203.0.113.1")  # 清零后放行


def test_real_redis_entry_limiter_blocks_at_limit(redis_store: RedisRateLimitStore) -> None:
    limiter = EntryRateLimiter(limit=2, window_seconds=60, too_many_message="慢", store=redis_store)
    limiter.hit("203.0.113.2")
    limiter.hit("203.0.113.2")
    with pytest.raises(HTTPException) as exc:
        limiter.hit("203.0.113.2")
    assert exc.value.status_code == 429
