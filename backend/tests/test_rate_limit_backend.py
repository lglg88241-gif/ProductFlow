"""限速存储后端与外观语义（审计 A1）。

覆盖：原子递增、TTL 固定窗口语义、达到上限 429、入口频率限制、check+record 并发时序
（线程模拟并发验证不超限穿透）、Redis 不可用 fail-closed（503 而非静默放行）。

后端选择语义：memory / auto（Redis 不可用显式降级 memory + warning）/
redis（不可用时 check/hit 必须 503，绝不静默回落 memory）。

真实 Redis 的集成测试见 test_rate_limit_redis_integration.py（按环境变量跳过）。
"""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastapi import HTTPException

from productflow_backend.infrastructure.rate_limit_backend import (
    InMemoryRateLimitStore,
    RedisRateLimitStore,
)
from productflow_backend.presentation.rate_limit import (
    DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE,
    SERVICE_UNAVAILABLE_MESSAGE,
    EntryRateLimiter,
    FailureRateLimiter,
    _entry_limit_from_env,
)

# 端口 1 无人监听：连接立即被拒，用于模拟 Redis 不可用（fail-closed 测试不依赖外部状态）
_BAD_REDIS_URL = "redis://127.0.0.1:1/0"


def _bad_redis_store() -> RedisRateLimitStore:
    return RedisRateLimitStore(_BAD_REDIS_URL, socket_connect_timeout=1.0, socket_timeout=1.0)


# ── InMemoryRateLimitStore：协议语义 ──


def test_inmemory_store_incr_is_atomic_under_threads() -> None:
    """原子递增：并发 INCR 不丢计数（memory 后端持锁，Redis 后端为 INCR 原子指令）。"""
    store = InMemoryRateLimitStore()
    workers, per_worker = 8, 25
    barrier = threading.Barrier(workers)

    def worker() -> None:
        barrier.wait(timeout=10)
        for _ in range(per_worker):
            store.incr("k", 60)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.get("k") == workers * per_worker


def test_inmemory_store_window_expires_and_restarts() -> None:
    """TTL 窗口语义：计数自首次写入起算，window 到期整体归零，窗口重新开始。"""
    store = InMemoryRateLimitStore()
    assert store.incr("k", 1) == 1
    assert store.get("k") == 1
    time.sleep(1.05)
    assert store.get("k") == 0, "TTL 到期后计数必须归零"
    assert store.incr("k", 1) == 1, "过期后重新计数，窗口重新开始"


def test_inmemory_store_ttl_is_not_refreshed_on_hit() -> None:
    """命中不得续期：持续爆破不能把固定窗口永久续命。"""
    store = InMemoryRateLimitStore()
    store.incr("k", 30)
    first_expiry = store._counts["k"][1]
    time.sleep(0.01)
    store.incr("k", 30)
    assert store._counts["k"][1] == first_expiry, "命中刷新了 TTL，窗口会被永久续期"


def test_inmemory_store_reset_all_only_touches_prefix() -> None:
    store = InMemoryRateLimitStore()
    store.incr("login:1.1.1.1", 60)
    store.incr("unlock:2.2.2.2", 60)
    store.reset_all("login:")
    assert store.get("login:1.1.1.1") == 0
    assert store.get("unlock:2.2.2.2") == 1


def test_inmemory_store_max_keys_eviction_keeps_memory_bounded() -> None:
    store = InMemoryRateLimitStore(max_keys=2)
    for index in range(5):
        store.incr(f"k{index}", 60)
    assert store.key_count("") <= 2, "内存键数量必须有上界"


# ── FailureRateLimiter 外观：接口保持不变 ──


def test_failure_limiter_allows_limit_failures_then_429() -> None:
    limiter = FailureRateLimiter(limit=3, window_seconds=90, too_many_message="太多", store=InMemoryRateLimitStore())
    for _ in range(3):
        limiter.check("1.1.1.1")
        limiter.record("1.1.1.1")
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.1.1.1")
    assert exc.value.status_code == 429
    assert exc.value.detail == "太多"
    assert int(exc.value.headers["Retry-After"]) >= 1


def test_failure_limiter_clear_resets_single_key() -> None:
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x", store=InMemoryRateLimitStore())
    limiter.record("1.1.1.1")
    limiter.record("1.1.1.1")
    with pytest.raises(HTTPException):
        limiter.check("1.1.1.1")
    limiter.clear("1.1.1.1")
    limiter.check("1.1.1.1")  # 成功清零后放行
    limiter.record("2.2.2.2")
    limiter.record("2.2.2.2")
    with pytest.raises(HTTPException):
        limiter.check("2.2.2.2")  # 2.2.2.2 已达上限
    limiter.clear("1.1.1.1")
    # clear 只作用于目标 key，不得复活其他 key 的计数
    with pytest.raises(HTTPException):
        limiter.check("2.2.2.2")


def test_failure_limiter_record_rejects_overflow() -> None:
    """原子 INCR 超限的失败请求立即 429——并发竞态放行的兜底。"""
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x", store=InMemoryRateLimitStore())
    limiter.record("k")
    limiter.record("k")
    with pytest.raises(HTTPException) as exc:
        limiter.record("k")
    assert exc.value.status_code == 429


def test_failure_limiter_tracked_key_count_is_namespaced_and_bounded() -> None:
    store = InMemoryRateLimitStore(max_keys=3)
    limiter = FailureRateLimiter(
        limit=5, window_seconds=60, too_many_message="x", max_keys=3, key_namespace="ns", store=store
    )
    for index in range(10):
        limiter.record(f"10.0.0.{index}")
    assert limiter.tracked_key_count() <= 3
    other = FailureRateLimiter(
        limit=5, window_seconds=60, too_many_message="x", key_namespace="other", store=store
    )
    assert other.tracked_key_count() == 0, "命名空间隔离：不应看到其他限速器的键"


def test_check_record_concurrency_never_exceeds_limit() -> None:
    """check+record 并发时序：线程模拟并发，验证不超限穿透。

    不变量：无论多少请求同时通过 check（竞态窗口），record 的原子 INCR 保证
    窗口内"未被 429 打断"的失败尝试恰好为 limit 个。
    """
    store = InMemoryRateLimitStore()
    limiter = FailureRateLimiter(limit=5, window_seconds=60, too_many_message="x", store=store)
    total, barrier, results_lock = 20, threading.Barrier(20), threading.Lock()
    results: list[str] = []

    def attempt() -> None:
        barrier.wait(timeout=10)
        outcome = "clean"
        try:
            limiter.check("1.2.3.4")
        except HTTPException:
            outcome = "blocked"
        else:
            try:
                limiter.record("1.2.3.4")
            except HTTPException:
                outcome = "overflow"
        with results_lock:
            results.append(outcome)

    threads = [threading.Thread(target=attempt) for _ in range(total)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    clean = results.count("clean")
    assert len(results) == total
    assert clean == 5, f"窗口内未被打断的失败尝试必须恰好为 limit，实际 {clean}"
    assert results.count("blocked") + results.count("overflow") == total - clean
    assert store.get("auth-failure:1.2.3.4") == clean + results.count("overflow")
    # 窗口已锁定：后续请求一律 429
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.2.3.4")
    assert exc.value.status_code == 429


# ── EntryRateLimiter：入口频率限制 ──


def test_entry_limiter_blocks_at_limit_plus_one() -> None:
    limiter = EntryRateLimiter(limit=3, window_seconds=60, too_many_message="慢", store=InMemoryRateLimitStore())
    for _ in range(3):
        limiter.hit("5.6.7.8")
    with pytest.raises(HTTPException) as exc:
        limiter.hit("5.6.7.8")
    assert exc.value.status_code == 429
    assert exc.value.detail == "慢"
    assert exc.value.headers["Retry-After"] == "60"


def test_entry_limiter_isolates_keys_and_counts_success_too() -> None:
    limiter = EntryRateLimiter(limit=2, window_seconds=60, too_many_message="慢", store=InMemoryRateLimitStore())
    limiter.hit("5.6.7.8")
    limiter.hit("5.6.7.8")
    with pytest.raises(HTTPException):
        limiter.hit("5.6.7.8")
    limiter.hit("9.9.9.9")  # 其他 IP 不受影响
    limiter.reset_all()
    limiter.hit("5.6.7.8")  # 复位后窗口重新开始


def test_entry_limiter_is_atomic_under_threads() -> None:
    """入口计数即判定：INCR 原子性保证超限请求必然被拒，无竞态放行。"""
    limiter = EntryRateLimiter(limit=10, window_seconds=60, too_many_message="慢", store=InMemoryRateLimitStore())
    total, barrier, allowed = 30, threading.Barrier(30), threading.Lock()
    passed = 0

    def attempt() -> None:
        nonlocal passed
        barrier.wait(timeout=10)
        try:
            limiter.hit("7.7.7.7")
        except HTTPException:
            return
        with allowed:
            passed += 1

    threads = [threading.Thread(target=attempt) for _ in range(total)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert passed == 10, f"并发下放行数必须恰好为 limit，实际 {passed}"


def test_entry_limit_from_env_defaults_and_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE == 20
    monkeypatch.delenv("AUTH_ENTRY_RATE_LIMIT_PER_MINUTE", raising=False)
    assert _entry_limit_from_env() == 20
    monkeypatch.setenv("AUTH_ENTRY_RATE_LIMIT_PER_MINUTE", "7")
    assert _entry_limit_from_env() == 7
    monkeypatch.setenv("AUTH_ENTRY_RATE_LIMIT_PER_MINUTE", "0")
    assert _entry_limit_from_env() == 1
    monkeypatch.setenv("AUTH_ENTRY_RATE_LIMIT_PER_MINUTE", "not-a-number")
    assert _entry_limit_from_env() == 20


# ── Redis 不可用 fail-closed（审计约束：不能静默回落 memory 取消防护）──


def test_failure_limiter_fails_closed_503_on_redis_outage() -> None:
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x", store=_bad_redis_store())
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.1.1.1")
    assert exc.value.status_code == 503
    assert exc.value.detail == SERVICE_UNAVAILABLE_MESSAGE
    # record 是记账不是闸门：Redis 抖动丢一条计数并告警，但 check 依旧 fail-closed
    limiter.record("1.1.1.1")
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.1.1.1")
    assert exc.value.status_code == 503


def test_entry_limiter_fails_closed_503_on_redis_outage() -> None:
    limiter = EntryRateLimiter(limit=3, window_seconds=60, too_many_message="慢", store=_bad_redis_store())
    with pytest.raises(HTTPException) as exc:
        limiter.hit("1.1.1.1")
    assert exc.value.status_code == 503
    assert exc.value.detail == SERVICE_UNAVAILABLE_MESSAGE


def test_backend_auto_falls_back_to_memory_with_warning(configured_env, monkeypatch, caplog) -> None:
    """auto：Redis 不可用时显式降级 memory 并打 warning——降级只发生在选择后端时。"""
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "auto")
    monkeypatch.setenv("REDIS_URL", _BAD_REDIS_URL)
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x")
    with caplog.at_level(logging.WARNING, logger="productflow_backend.presentation.rate_limit"):
        limiter.check("1.1.1.1")  # 不应 503：auto 已降级 memory
        limiter.record("1.1.1.1")
    assert isinstance(limiter._resolved_store, InMemoryRateLimitStore)
    assert any("降级" in record.message for record in caplog.records), "auto 降级必须留痕"


def test_backend_explicit_redis_never_falls_back_to_memory(configured_env, monkeypatch) -> None:
    """审计约束：显式选择 redis 后端 + 坏连接串 → check 必须 503，绝不静默放行。"""
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "redis")
    monkeypatch.setenv("REDIS_URL", _BAD_REDIS_URL)
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x")
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.1.1.1")
    assert exc.value.status_code == 503
    assert exc.value.detail == SERVICE_UNAVAILABLE_MESSAGE


def test_backend_invalid_choice_treated_as_auto(configured_env, monkeypatch, caplog) -> None:
    monkeypatch.setenv("RATE_LIMIT_BACKEND", "banana")
    monkeypatch.setenv("REDIS_URL", _BAD_REDIS_URL)
    limiter = FailureRateLimiter(limit=2, window_seconds=60, too_many_message="x")
    with caplog.at_level(logging.WARNING, logger="productflow_backend.presentation.rate_limit"):
        limiter.check("1.1.1.1")
    assert isinstance(limiter._resolved_store, InMemoryRateLimitStore)
    assert any("banana" in record.message for record in caplog.records)


# ── 端到端：入口限速与 fail-closed 走完整 HTTP 栈 ──


def test_login_endpoint_entry_rate_limit_returns_429(configured_env, monkeypatch) -> None:
    from productflow_backend.presentation.api import create_app
    from productflow_backend.presentation.rate_limit import InMemoryRateLimitStore
    from productflow_backend.presentation.routes import auth as auth_module

    monkeypatch.setattr(
        auth_module,
        "entry_rate_limiter",
        EntryRateLimiter(
            limit=3,
            window_seconds=60,
            too_many_message="请求过于频繁，请稍后再试",
            store=InMemoryRateLimitStore(),
        ),
    )
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    for _ in range(3):
        assert client.post("/api/auth/session", json={"admin_key": "definitely-wrong"}).status_code == 401
    limited = client.post("/api/auth/session", json={"admin_key": "definitely-wrong"})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "请求过于频繁，请稍后再试"
    assert int(limited.headers["Retry-After"]) >= 1


def test_unlock_endpoint_entry_rate_limit_returns_429(configured_env, monkeypatch) -> None:
    from productflow_backend.presentation.api import create_app
    from productflow_backend.presentation.rate_limit import InMemoryRateLimitStore
    from productflow_backend.presentation.routes import settings as settings_module

    monkeypatch.setattr(
        settings_module,
        "entry_rate_limiter",
        EntryRateLimiter(
            limit=2,
            window_seconds=60,
            too_many_message="请求过于频繁，请稍后再试",
            store=InMemoryRateLimitStore(),
        ),
    )
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    assert client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"}).status_code == 200
    for _ in range(2):
        assert client.post("/api/settings/unlock", json={"token": "definitely-wrong"}).status_code == 401
    limited = client.post("/api/settings/unlock", json={"token": "definitely-wrong"})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "请求过于频繁，请稍后再试"


def test_login_endpoint_fails_closed_503_when_redis_unreachable(configured_env, monkeypatch) -> None:
    """审计 A1 端到端：登录入口选择 Redis 后端且 Redis 不可用 → 503，密钥正确也不放行。"""
    from productflow_backend.presentation.api import create_app
    from productflow_backend.presentation.routes import auth as auth_module

    monkeypatch.setattr(auth_module.login_rate_limiter, "_explicit_store", _bad_redis_store())
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    response = client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"})
    assert response.status_code == 503
    assert SERVICE_UNAVAILABLE_MESSAGE in response.json()["detail"]
    assert client.post("/api/auth/session", json={"admin_key": "definitely-wrong"}).status_code == 503


def test_unlock_endpoint_fails_closed_503_when_redis_unreachable(configured_env, monkeypatch) -> None:
    from productflow_backend.presentation import rate_limit as rate_limit_module
    from productflow_backend.presentation.api import create_app

    monkeypatch.setattr(rate_limit_module.settings_unlock_rate_limiter, "_explicit_store", _bad_redis_store())
    from fastapi.testclient import TestClient

    client = TestClient(create_app())
    assert client.post("/api/auth/session", json={"admin_key": "super-secret-admin-key"}).status_code == 200
    response = client.post("/api/settings/unlock", json={"token": "super-secret-settings-token"})
    assert response.status_code == 503
    assert SERVICE_UNAVAILABLE_MESSAGE in response.json()["detail"]
