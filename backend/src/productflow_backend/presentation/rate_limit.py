"""登录/解锁失败限速与入口频率限制：外观层，计数委托 RateLimitStore（审计 A1）。

历史缺陷：限速状态存进程内存——多进程/多实例不共享计数、重启清零，爆破防护可被横向绕过。

现在（审计 A1）：``FailureRateLimiter`` 变为外观（facade），固定窗口计数委托
``infrastructure.rate_limit_backend`` 的内存/Redis 存储。后端由环境变量
``RATE_LIMIT_BACKEND``（``auto``|``memory``|``redis``，默认 ``auto``）选择：

- ``auto``：首选 Redis（``get_settings().redis_url``），不可用时**显式降级** memory 并打
  warning 日志（降级只发生在选择后端时，不发生在调用时）；
- ``redis``：只走 Redis。调用时不可用 → ``check()``/``hit()`` 抛 503 fail-closed，
  **绝不静默回落 memory 取消防护**；
- ``memory``：单实例语义（测试与显式降级用）。

语义约束（审计原文）：选 Redis 后端后 Redis 不可用时必须 503，不能静默放行。
``record()`` 是记账而非闸门（``check()`` 已经拦在前面），Redis 抖动时丢一条计数并打
warning 即可——下一次 ``check()`` 会继续 fail-closed。

语义变化（滑动窗口 → 固定窗口）：存储为 INCR+TTL 计数器，窗口自首次计数起算。
并发下 ``check()`` 存在竞态放行，但 ``record()`` 的原子 INCR 保证：超过上限的失败
请求在记账时立即被 429，窗口内未被限速打断的失败尝试总数不超过 limit。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading

from fastapi import HTTPException, Request, status

from productflow_backend.config import get_settings
from productflow_backend.infrastructure.rate_limit_backend import (
    DEFAULT_MAX_TRACKED_KEYS,
    InMemoryRateLimitStore,
    RateLimitStore,
    RedisRateLimitStore,
)

logger = logging.getLogger(__name__)

# Redis 后端不可用时的统一口径（fail-closed，审计 A1）
SERVICE_UNAVAILABLE_MESSAGE = "登录/解锁服务暂时不可用，请稍后再试"

_RATE_LIMIT_BACKEND_ENV = "RATE_LIMIT_BACKEND"
_VALID_BACKENDS = {"auto", "memory", "redis"}

# 入口频率限制：每 IP 每分钟请求数（登录与解锁入口），可用 AUTH_ENTRY_RATE_LIMIT_PER_MINUTE 调整
ENTRY_RATE_LIMIT_ENV = "AUTH_ENTRY_RATE_LIMIT_PER_MINUTE"
DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE = 20
_ENTRY_WINDOW_SECONDS = 60
_LOGIN_WINDOW_SECONDS = 15 * 60


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


def _backend_choice() -> str:
    """读取 RATE_LIMIT_BACKEND（auto|memory|redis，默认 auto），非法值按 auto 并告警。"""
    raw = (os.getenv(_RATE_LIMIT_BACKEND_ENV) or "auto").strip().lower()
    if raw in _VALID_BACKENDS:
        return raw
    logger.warning("%s=%r 不是 auto/memory/redis 之一，按 auto 处理", _RATE_LIMIT_BACKEND_ENV, raw)
    return "auto"


def _create_store_from_env(*, max_keys: int) -> tuple[RateLimitStore, bool]:
    """按环境选择限速存储；返回 (store, 是否选定 Redis 后端)。

    - memory → 内存存储；
    - redis → 只构造 Redis 存储，不做可用性探测、绝不回落 memory；
      不可用由调用时的 fail-closed（503）兜住；
    - auto → 探测 Redis，失败显式降级 memory 并打 warning。
    """
    choice = _backend_choice()
    if choice == "memory":
        return InMemoryRateLimitStore(max_keys=max_keys), False
    if choice == "redis":
        return RedisRateLimitStore(get_settings().redis_url), True
    try:
        store = RedisRateLimitStore(get_settings().redis_url)
        store.ping()
    except Exception as exc:  # auto 模式：任何连接类失败都显式降级，但必须留痕
        logger.warning(
            "RATE_LIMIT_BACKEND=auto：Redis 不可用（%s: %s），降级为进程内存限速——"
            "多实例部署下计数不共享、重启清零；如需共享请检查 REDIS_URL 或改用 RATE_LIMIT_BACKEND=redis",
            type(exc).__name__,
            exc,
        )
        return InMemoryRateLimitStore(max_keys=max_keys), False
    return store, True


class _StoreBackedLimiter:
    """共享的存储解析与 fail-closed 语义（审计 A1）。"""

    def __init__(
        self,
        *,
        key_namespace: str,
        max_keys: int,
        store: RateLimitStore | None = None,
    ) -> None:
        self._namespace = key_namespace
        self._max_keys = max(1, max_keys)
        # 显式注入的存储（测试替身/集成测试）优先；否则首次使用时按环境惰性解析
        self._explicit_store = store
        self._resolved_store: RateLimitStore | None = None
        self._store_lock = threading.Lock()

    @property
    def _store(self) -> RateLimitStore:
        if self._explicit_store is not None:
            return self._explicit_store
        if self._resolved_store is None:
            with self._store_lock:
                if self._resolved_store is None:
                    self._resolved_store, _ = _create_store_from_env(max_keys=self._max_keys)
        return self._resolved_store

    def _key(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    def _fails_closed(self) -> bool:
        """Redis 后端（显式指定或解析失败于 redis 模式）→ 调用失败必须 503，不能静默放行。"""
        store = self._explicit_store if self._explicit_store is not None else self._resolved_store
        if isinstance(store, RedisRateLimitStore):
            return True
        # 存储尚未成功解析（解析即失败）：按当前环境声明是否选择了 redis 后端
        return store is None and self._explicit_store is None and _backend_choice() == "redis"

    def _store_call(self, method: str, *args, fail_closed: bool):
        try:
            return getattr(self._store, method)(*args)
        except HTTPException:
            raise
        except Exception as exc:
            if not self._fails_closed():
                raise
            if not fail_closed:
                # 记账类操作（record/clear）：check 才是闸门，丢一条计数但保持服务可用
                logger.warning(
                    "限速 Redis 写入失败（%s: %s）——本次计数丢失，后续 check 仍 fail-closed",
                    type(exc).__name__,
                    exc,
                )
                return None
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=SERVICE_UNAVAILABLE_MESSAGE,
            ) from exc

    def tracked_key_count(self) -> int:
        """当前跟踪的键数量（供容量守卫与测试断言；存储需实现 key_count 扩展）。"""
        counter = getattr(self._store, "key_count", None)
        return int(counter(self._key(""))) if counter else 0

    def reset_all(self) -> None:
        """测试专用：清空本限速器命名空间下的全部计数。"""
        self._store.reset_all(self._key(""))

    def reset_backend_resolution(self) -> None:
        """测试专用：丢弃惰性解析出的存储，下次调用按当前环境重新选择后端。"""
        with self._store_lock:
            self._resolved_store = None


class FailureRateLimiter(_StoreBackedLimiter):
    """按 key（通常是客户端 IP）记录失败次数，窗口内达到上限抛 429。

    外观接口与原实现完全一致（check/record/clear/reset_all/tracked_key_count），
    计数委托 RateLimitStore；选择 Redis 后端后 Redis 不可用时 check() 抛 503。
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        too_many_message: str,
        max_keys: int = DEFAULT_MAX_TRACKED_KEYS,
        key_namespace: str = "auth-failure",
        store: RateLimitStore | None = None,
    ) -> None:
        super().__init__(key_namespace=key_namespace, max_keys=max_keys, store=store)
        self._limit = max(1, limit)
        self._window = window_seconds
        self._message = too_many_message

    def _too_many(self) -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=self._message,
            headers={"Retry-After": str(self._window)},
        )

    def check(self, key: str) -> None:
        """未达上限直接返回；达到上限抛 429（带 Retry-After）。

        Redis 后端不可用时抛 503（fail-closed）——不能为了可用性取消防护。
        """
        count = self._store_call("get", self._key(key), fail_closed=True)
        if int(count) < self._limit:
            return
        raise self._too_many()

    def record(self, key: str) -> None:
        """原子递增失败计数；并发下 INCR 结果超过上限的请求立即 429。

        这是 check() 竞态放行的兜底：无论多少请求同时通过 check，
        窗口内"未被限速打断"的失败尝试总数不超过 limit。
        """
        count = self._store_call("incr", self._key(key), self._window, fail_closed=False)
        if count is not None and int(count) > self._limit:
            raise self._too_many()

    def clear(self, key: str) -> None:
        """成功后清零该 key 的失败计数。"""
        self._store_call("delete", self._key(key), fail_closed=False)


class EntryRateLimiter(_StoreBackedLimiter):
    """入口频率限制：固定窗口，每 key 每 window 最多 limit 次请求，超限 429。

    与失败限速不同：这里计数**所有**入口请求（含成功），判定就是一次原子 INCR——
    计数超过上限的请求本身即被拒绝，天然并发安全，无需 check/record 两段式。
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: int,
        too_many_message: str,
        max_keys: int = DEFAULT_MAX_TRACKED_KEYS,
        key_namespace: str = "auth-entry",
        store: RateLimitStore | None = None,
    ) -> None:
        super().__init__(key_namespace=key_namespace, max_keys=max_keys, store=store)
        self._limit = max(1, limit)
        self._window = window_seconds
        self._message = too_many_message

    def hit(self, key: str) -> None:
        """每次入口请求调用：原子计数，窗口内第 limit+1 次起抛 429。

        Redis 后端不可用时抛 503（fail-closed）。
        """
        count = self._store_call("incr", self._key(key), self._window, fail_closed=True)
        if int(count) > self._limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=self._message,
                headers={"Retry-After": str(self._window)},
            )


def _entry_limit_from_env() -> int:
    """读取 AUTH_ENTRY_RATE_LIMIT_PER_MINUTE（默认 20），非法值回退默认并告警。"""
    raw = (os.getenv(ENTRY_RATE_LIMIT_ENV) or "").strip()
    if not raw:
        return DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("%s=%r 不是整数，使用默认 %d", ENTRY_RATE_LIMIT_ENV, raw, DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE)
        return DEFAULT_ENTRY_RATE_LIMIT_PER_MINUTE


# 管理员登录：15 分钟窗口内第 6 次失败起 429
login_rate_limiter = FailureRateLimiter(
    limit=5,
    window_seconds=_LOGIN_WINDOW_SECONDS,
    too_many_message="登录失败次数过多，请 15 分钟后再试",
    key_namespace="login-failures",
)

# 设置页二次令牌解锁：与登录同一策略（此前可无限爆破）
settings_unlock_rate_limiter = FailureRateLimiter(
    limit=5,
    window_seconds=_LOGIN_WINDOW_SECONDS,
    too_many_message="设置解锁尝试过多，请 15 分钟后再试",
    key_namespace="settings-unlock-failures",
)

# 登录/解锁入口频率限制：每 IP 每分钟默认 20 次（含失败与成功）
entry_rate_limiter = EntryRateLimiter(
    limit=_entry_limit_from_env(),
    window_seconds=_ENTRY_WINDOW_SECONDS,
    too_many_message="请求过于频繁，请稍后再试",
    key_namespace="auth-entry",
)

# 用户账号入口（批次 B）：用户登录与邀请兑换的独立入口限速，避免与 admin 入口互相挤占
user_login_entry_rate_limiter = EntryRateLimiter(
    limit=_entry_limit_from_env(),
    window_seconds=_ENTRY_WINDOW_SECONDS,
    too_many_message="登录请求过于频繁，请稍后再试",
    key_namespace="user-login-entry",
)

user_redeem_entry_rate_limiter = EntryRateLimiter(
    limit=_entry_limit_from_env(),
    window_seconds=_ENTRY_WINDOW_SECONDS,
    too_many_message="兑换请求过于频繁，请稍后再试",
    key_namespace="user-redeem-entry",
)


def reset_all_rate_limiters() -> None:
    """测试专用：重置全部模块级限速器状态与惰性后端解析。

    只丢弃惰性解析出的存储（丢弃即等于清空其全部状态），不触碰显式注入的存储
    （由注入它的测试自己负责），也不发起任何存储连接——teardown 阶段 env 可能
    已指向不可用的 Redis，此时绝不能再去连接。
    """
    for limiter in (
        login_rate_limiter,
        settings_unlock_rate_limiter,
        entry_rate_limiter,
        user_login_entry_rate_limiter,
        user_redeem_entry_rate_limiter,
    ):
        limiter.reset_backend_resolution()
