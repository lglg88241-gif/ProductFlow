"""远程图片下载的 SSRF 安全封装。

生图中转站返回的图片 URL 属于不可信输入：历史上曾用 ``httpx.get(..., follow_redirects=True)``
裸下载，存在以下风险：

- 无协议白名单（file://、gopher:// 等都可能被传入）；
- 无内网 IP 校验，DNS 重绑定 / 内网域名可以把请求打进 private/loopback 网段；
- 重定向可跳内网（每一跳的 host 都必须重新校验）；
- 响应体无大小上限，恶意服务器可以用无限流撑爆内存。

本模块提供 :func:`fetch_remote_image` 统一收口：仅允许 http/https，每一跳先做 DNS
解析并对全部 IPv4/IPv6 结果做私网网段校验，手动跟随重定向并逐跳复检，流式读取强制
字节上限，Content-Type 必须为 image/*（缺失时宽松放行）。

本地/测试逃生口：环境变量 ``REMOTE_FETCH_ALLOW_PRIVATE_NETWORK=1`` 时跳过私网校验
（默认拒绝）。
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_TIMEOUT_SECONDS = 120.0
STREAM_CHUNK_SIZE = 64 * 1024

ALLOW_PRIVATE_NETWORK_ENV = "REMOTE_FETCH_ALLOW_PRIVATE_NETWORK"
ALLOWED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


class RemoteFetchError(Exception):
    """远程图片下载失败，携带机器可读的 ``reason``。"""

    def __init__(self, message: str, *, reason: str = "unknown") -> None:
        super().__init__(message)
        self.reason = reason


def allow_private_network() -> bool:
    """是否放行私网目标（仅供本地/测试，默认拒绝）。"""
    return os.getenv(ALLOW_PRIVATE_NETWORK_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def _is_blocked_address(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """任一命中保留网段即视为不安全（IPv4 与 IPv6 语义由 ipaddress 统一处理）。"""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_host_ips(hostname: str) -> list[str]:
    """解析主机名的全部 IP（去重保序）；解析失败按拒绝处理。"""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise RemoteFetchError(f"无法解析远程图片主机名: {hostname}", reason="dns_error") from exc
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0])
        if ip_text and ip_text not in ips:
            ips.append(ip_text)
    return ips


def _check_url_scheme(url: str) -> str:
    split = urlsplit(url)
    scheme = (split.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise RemoteFetchError(
            f"远程图片仅允许 http/https 协议，收到: {scheme or '(缺失)'}", reason="scheme_not_allowed"
        )
    if not split.hostname:
        raise RemoteFetchError("远程图片 URL 缺少主机名", reason="invalid_url")
    return split.hostname


def _assert_host_is_public(hostname: str) -> None:
    """DNS 解析到的任一 IP 命中保留网段即拒绝；显式逃生口放行。"""
    if allow_private_network():
        return
    for ip_text in _resolve_host_ips(hostname):
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            # 解析结果不是合法 IP（罕见），宁可拒绝
            raise RemoteFetchError(f"远程图片主机解析结果异常: {ip_text}", reason="dns_error") from None
        if _is_blocked_address(ip):
            raise RemoteFetchError(
                f"远程图片主机解析到受限网段，已拒绝下载: {ip_text}",
                reason="private_network_blocked",
            )


def _read_payload_limited(response: httpx.Response, *, max_bytes: int) -> bytes:
    """流式读取响应体并强制字节上限，避免恶意服务器撑爆内存。"""
    payload = bytearray()
    try:
        for chunk in response.iter_bytes(chunk_size=STREAM_CHUNK_SIZE):
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise RemoteFetchError(
                    f"远程图片响应体超过大小上限（>{max_bytes} 字节）",
                    reason="payload_too_large",
                )
    except httpx.HTTPError as exc:
        raise RemoteFetchError(f"远程图片下载中断: {type(exc).__name__}", reason="network_error") from exc
    return bytes(payload)


def _check_content_type(response: httpx.Response) -> None:
    """Content-Type 应为 image/*；缺失时宽松放行。"""
    content_type = (response.headers.get("content-type") or "").strip()
    if not content_type:
        return
    media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
    if media_type and not media_type.startswith("image/"):
        raise RemoteFetchError(
            f"远程图片 Content-Type 不是 image/*: {media_type}",
            reason="content_type_not_image",
        )


def fetch_remote_image(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
) -> bytes:
    """安全下载远程图片字节流。

    - 仅允许 http/https；
    - 每一跳先 DNS 解析并校验全部 IP（私网/回环/链路本地/保留/组播/未指定均拒绝）；
    - 手动跟随重定向（最多 ``max_redirects`` 跳），每一跳重新做协议与 IP 校验；
    - 流式读取并强制 ``max_bytes`` 上限；
    - Content-Type 应为 image/*（缺失时宽松放行）；
    - 所有失败统一抛出 :class:`RemoteFetchError`。
    """
    if max_bytes <= 0:
        raise RemoteFetchError("max_bytes 必须为正数", reason="invalid_arguments")
    if max_redirects < 0:
        raise RemoteFetchError("max_redirects 不能为负数", reason="invalid_arguments")

    current_url = url
    for redirect_index in range(max_redirects + 1):
        hostname = _check_url_scheme(current_url)
        _assert_host_is_public(hostname)

        try:
            with httpx.stream(
                "GET",
                current_url,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                if response.status_code in _REDIRECT_STATUS_CODES:
                    if redirect_index >= max_redirects:
                        raise RemoteFetchError(
                            f"远程图片重定向次数超过上限（>{max_redirects} 跳）",
                            reason="too_many_redirects",
                        )
                    location = (response.headers.get("location") or "").strip()
                    if not location:
                        raise RemoteFetchError("远程图片重定向缺少 Location 头", reason="invalid_redirect")
                    next_url = urljoin(current_url, location)
                    logger.info(
                        "远程图片重定向: from=%s to=%s hop=%s",
                        current_url[:160],
                        next_url[:160],
                        redirect_index + 1,
                    )
                    current_url = next_url
                    continue

                if response.status_code >= 400:
                    raise RemoteFetchError(
                        f"远程图片下载失败，HTTP 状态码: {response.status_code}",
                        reason="http_error",
                    )

                _check_content_type(response)
                return _read_payload_limited(response, max_bytes=max_bytes)
        except RemoteFetchError:
            raise
        except httpx.HTTPError as exc:
            raise RemoteFetchError(
                f"远程图片请求失败: {type(exc).__name__}",
                reason="network_error",
            ) from exc

    raise RemoteFetchError("远程图片重定向未收敛", reason="too_many_redirects")
