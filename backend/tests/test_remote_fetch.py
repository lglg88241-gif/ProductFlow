"""remote_fetch 的 SSRF 防护单元测试：monkeypatch httpx 与 socket.getaddrinfo。"""

from __future__ import annotations

import socket
from typing import Any

import httpx as real_httpx
import pytest

from productflow_backend.infrastructure import remote_fetch
from productflow_backend.infrastructure.image import images_provider
from productflow_backend.infrastructure.remote_fetch import RemoteFetchError, fetch_remote_image

PUBLIC_IPV4 = "93.184.216.34"
PUBLIC_IPV6 = "2606:4700::1111"

DNS_MAP: dict[str, list[str]] = {
    "cdn.example.com": [PUBLIC_IPV4],
    "v6.example.com": [PUBLIC_IPV6],
    "internal.example.com": ["10.1.2.3"],
    "loopback.example.com": ["127.0.0.1"],
    "linklocal.example.com": ["169.254.1.1"],
    "reserved.example.com": ["240.0.0.1"],
    "multicast.example.com": ["224.0.0.1"],
    "unspecified.example.com": ["0.0.0.0"],
    "ula.example.com": ["fc00::1"],
    "rebind.example.com": [PUBLIC_IPV4, "127.0.0.1"],
    "multihomed.example.com": [PUBLIC_IPV4, PUBLIC_IPV6],
}


def _install_dns(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]] | None = None) -> None:
    effective = mapping if mapping is not None else DNS_MAP

    def fake_getaddrinfo(host: str, port: Any, *args: Any) -> list[tuple[Any, ...]]:
        ips = effective.get(host)
        if ips is None:
            raise socket.gaierror(-2, "Name or service not known")
        infos: list[tuple[Any, ...]] = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
            infos.append((family, socket.SOCK_STREAM, 6, "", sockaddr))
        return infos

    monkeypatch.setattr(remote_fetch.socket, "getaddrinfo", fake_getaddrinfo)


class FakeStreamResponse:
    def __init__(self, *, status_code: int, headers: dict[str, str] | None, chunks: list[bytes]) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks

    def iter_bytes(self, chunk_size: int | None = None) -> list[bytes]:
        return list(self._chunks)


class FakeStreamContext:
    def __init__(self, response: FakeStreamResponse) -> None:
        self.response = response

    def __enter__(self) -> FakeStreamResponse:
        return self.response

    def __exit__(self, *args: Any) -> bool:
        return False


class FakeHttpx:
    """按 URL 返回预设响应，并记录每次请求参数。"""

    # remote_fetch 的异常处理引用了 httpx 的异常类，fake 需要透传真实类型
    HTTPError = real_httpx.HTTPError

    def __init__(self, responses: dict[str, FakeStreamResponse]) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def stream(self, method: str, url: str, *, timeout: Any = None, follow_redirects: bool = False):
        self.requests.append({"method": method, "url": url, "follow_redirects": follow_redirects})
        response = self.responses.get(url)
        if response is None:
            raise AssertionError(f"unexpected request: {method} {url}")
        return FakeStreamContext(response)


def _image_response(*, chunks: list[bytes] | None = None, content_type: str | None = "image/png") -> FakeStreamResponse:
    return FakeStreamResponse(
        status_code=200,
        headers={} if content_type is None else {"content-type": content_type},
        chunks=chunks if chunks is not None else [b"\x89PNG-fake-bytes"],
    )


def _redirect_response(location: str) -> FakeStreamResponse:
    return FakeStreamResponse(status_code=302, headers={"location": location}, chunks=[])


def _install_httpx(monkeypatch: pytest.MonkeyPatch, fake: FakeHttpx) -> FakeHttpx:
    monkeypatch.setattr(remote_fetch, "httpx", fake)
    return fake


def test_fetch_rejects_non_http_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    fake = _install_httpx(monkeypatch, FakeHttpx({}))

    for url in ("file:///etc/passwd", "ftp://cdn.example.com/a.png", "gopher://cdn.example.com"):
        with pytest.raises(RemoteFetchError) as exc_info:
            fetch_remote_image(url)
        assert exc_info.value.reason == "scheme_not_allowed"
    assert fake.requests == []


def test_fetch_downloads_public_image_with_manual_redirect_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    fake = _install_httpx(monkeypatch, FakeHttpx({"https://cdn.example.com/a.png": _image_response()}))

    payload = fetch_remote_image("https://cdn.example.com/a.png")

    assert payload == b"\x89PNG-fake-bytes"
    assert fake.requests == [
        {"method": "GET", "url": "https://cdn.example.com/a.png", "follow_redirects": False}
    ]


def test_fetch_accepts_public_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(monkeypatch, FakeHttpx({"https://v6.example.com/a.png": _image_response()}))

    assert fetch_remote_image("https://v6.example.com/a.png") == b"\x89PNG-fake-bytes"


@pytest.mark.parametrize(
    ("host", "ip"),
    [
        ("internal.example.com", "10.1.2.3"),
        ("loopback.example.com", "127.0.0.1"),
        ("linklocal.example.com", "169.254.1.1"),
        ("reserved.example.com", "240.0.0.1"),
        ("multicast.example.com", "224.0.0.1"),
        ("unspecified.example.com", "0.0.0.0"),
        ("ula.example.com", "fc00::1"),
        ("127.0.0.1", "127.0.0.1"),
    ],
)
def test_fetch_blocks_private_network_targets(
    monkeypatch: pytest.MonkeyPatch, host: str, ip: str
) -> None:
    _install_dns(monkeypatch, {host: [ip]})
    fake = _install_httpx(monkeypatch, FakeHttpx({}))

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image(f"https://{host}/a.png")

    assert exc_info.value.reason == "private_network_blocked"
    assert fake.requests == []


def test_fetch_blocks_dns_rebinding_with_mixed_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    # 任一 IP 命中私网即拒绝（防止解析到公网+内网多地址时漏放）
    _install_dns(monkeypatch)
    fake = _install_httpx(monkeypatch, FakeHttpx({}))

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://rebind.example.com/a.png")

    assert exc_info.value.reason == "private_network_blocked"
    assert fake.requests == []


def test_fetch_follows_redirects_and_rechecks_each_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    fake = _install_httpx(
        monkeypatch,
        FakeHttpx(
            {
                "https://cdn.example.com/redirect": _redirect_response("/final.png"),
                "https://cdn.example.com/final.png": _image_response(),
            }
        ),
    )

    payload = fetch_remote_image("https://cdn.example.com/redirect")

    assert payload == b"\x89PNG-fake-bytes"
    assert [request["url"] for request in fake.requests] == [
        "https://cdn.example.com/redirect",
        "https://cdn.example.com/final.png",
    ]


def test_fetch_blocks_redirect_into_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    fake = _install_httpx(
        monkeypatch,
        FakeHttpx({"https://cdn.example.com/redirect": _redirect_response("http://internal.example.com/a.png")}),
    )

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://cdn.example.com/redirect")

    assert exc_info.value.reason == "private_network_blocked"
    # 重定向目标必须未被实际请求
    assert [request["url"] for request in fake.requests] == ["https://cdn.example.com/redirect"]


def test_fetch_rejects_redirects_beyond_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    fake = _install_httpx(
        monkeypatch,
        FakeHttpx(
            {
                "https://cdn.example.com/hop0": _redirect_response("/hop1"),
                "https://cdn.example.com/hop1": _redirect_response("/hop2"),
                "https://cdn.example.com/hop2": _redirect_response("/hop3"),
                "https://cdn.example.com/hop3": _redirect_response("/hop4"),
            }
        ),
    )

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://cdn.example.com/hop0", max_redirects=3)

    assert exc_info.value.reason == "too_many_redirects"
    assert len(fake.requests) == 4


def test_fetch_rejects_payload_over_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(
        monkeypatch,
        FakeHttpx({"https://cdn.example.com/big.png": _image_response(chunks=[b"a" * 8, b"b" * 8])}),
    )

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://cdn.example.com/big.png", max_bytes=10)

    assert exc_info.value.reason == "payload_too_large"


def test_fetch_rejects_non_image_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(
        monkeypatch,
        FakeHttpx({"https://cdn.example.com/page": _image_response(content_type="text/html")}),
    )

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://cdn.example.com/page")

    assert exc_info.value.reason == "content_type_not_image"


def test_fetch_allows_missing_content_type(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(
        monkeypatch,
        FakeHttpx({"https://cdn.example.com/a.png": _image_response(content_type=None)}),
    )

    assert fetch_remote_image("https://cdn.example.com/a.png") == b"\x89PNG-fake-bytes"


def test_fetch_maps_http_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(
        monkeypatch,
        FakeHttpx(
            {
                "https://cdn.example.com/missing.png": FakeStreamResponse(
                    status_code=404, headers={"content-type": "image/png"}, chunks=[]
                )
            }
        ),
    )

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://cdn.example.com/missing.png")

    assert exc_info.value.reason == "http_error"


def test_fetch_maps_dns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch, {})
    _install_httpx(monkeypatch, FakeHttpx({}))

    with pytest.raises(RemoteFetchError) as exc_info:
        fetch_remote_image("https://no-such-host.invalid/a.png")

    assert exc_info.value.reason == "dns_error"


def test_fetch_escape_hatch_allows_private_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_dns(monkeypatch)
    _install_httpx(
        monkeypatch,
        FakeHttpx({"http://internal.example.com/a.png": _image_response()}),
    )
    monkeypatch.setenv("REMOTE_FETCH_ALLOW_PRIVATE_NETWORK", "1")

    assert fetch_remote_image("http://internal.example.com/a.png") == b"\x89PNG-fake-bytes"


def test_download_remote_image_retries_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images_provider, "EMPTY_OUTPUT_RETRY_DELAY_SECONDS", 0)
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs: Any) -> bytes:
        calls.append(url)
        if len(calls) < 3:
            raise RemoteFetchError("暂时失败", reason="network_error")
        return b"image-bytes"

    monkeypatch.setattr(images_provider, "fetch_remote_image", fake_fetch)

    assert images_provider._download_remote_image("https://cdn.example.com/a.png") == b"image-bytes"
    assert len(calls) == 3


def test_download_remote_image_returns_none_after_retries_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(images_provider, "EMPTY_OUTPUT_RETRY_DELAY_SECONDS", 0)
    calls: list[str] = []

    def fake_fetch(url: str, **kwargs: Any) -> bytes:
        calls.append(url)
        raise RemoteFetchError("私网拒绝", reason="private_network_blocked")

    monkeypatch.setattr(images_provider, "fetch_remote_image", fake_fetch)

    assert images_provider._download_remote_image("https://cdn.example.com/a.png") is None
    assert len(calls) == images_provider.REMOTE_IMAGE_DOWNLOAD_RETRIES + 1


def test_download_remote_image_retries_on_empty_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(images_provider, "EMPTY_OUTPUT_RETRY_DELAY_SECONDS", 0)
    payloads: list[bytes] = [b"", b"final-bytes"]

    def fake_fetch(url: str, **kwargs: Any) -> bytes:
        return payloads.pop(0)

    monkeypatch.setattr(images_provider, "fetch_remote_image", fake_fetch)

    assert images_provider._download_remote_image("https://cdn.example.com/a.png") == b"final-bytes"
