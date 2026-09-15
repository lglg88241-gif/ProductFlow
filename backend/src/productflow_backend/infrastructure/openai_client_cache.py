"""进程内重型客户端实例缓存（OpenAI SDK 客户端等）。

OpenAI SDK 客户端创建成本高（底层各自持有 httpx 连接池），且线程安全、可并发复用。
此前 agent 轮次 / 图片供应商每次调用都新建客户端，等于每请求重建连接池。
这里提供加锁的 dict 缓存：按连接参数（api_key/base_url/超时预算）复用同一实例。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Hashable
from typing import Any

# 进程内所有缓存实例：测试间统一清空，保证 monkeypatch 的假客户端每次都被重新构造
_ALL_CACHES: list[KeyedClientCache] = []
_REGISTRY_LOCK = threading.Lock()


class KeyedClientCache:
    """按不可变连接参数键缓存客户端实例；get_or_create 线程安全。"""

    def __init__(self) -> None:
        self._clients: dict[Hashable, Any] = {}
        self._lock = threading.Lock()
        with _REGISTRY_LOCK:
            _ALL_CACHES.append(self)

    def get_or_create(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = factory()
                self._clients[key] = client
            return client

    def clear(self) -> None:
        with self._lock:
            self._clients.clear()


def clear_all_caches() -> None:
    with _REGISTRY_LOCK:
        caches = list(_ALL_CACHES)
    for cache in caches:
        cache.clear()
