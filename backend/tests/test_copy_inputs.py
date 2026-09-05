from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import _login, _make_demo_image_bytes

from productflow_backend.infrastructure.copy_inputs import OcrUnavailableError, normalize_ocr_result


@pytest.fixture()
def client(configured_env: Path) -> TestClient:
    from productflow_backend.presentation.api import create_app

    api_client = TestClient(create_app())
    _login(api_client)
    return api_client


def test_extract_copy_input_decodes_utf8_and_gb18030(client: TestClient) -> None:
    utf8_response = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("copy.txt", "主标题\r\n卖点一".encode("utf-8-sig"), "text/plain")},
    )
    gb18030_response = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("copy.md", "副标题\n卖点二".encode("gb18030"), "application/octet-stream")},
    )

    assert utf8_response.status_code == 200
    assert utf8_response.json() == {
        "text": "主标题\n卖点一",
        "source_kind": "text",
        "warnings": [],
    }
    assert gb18030_response.status_code == 200
    assert gb18030_response.json()["text"] == "副标题\n卖点二"
    assert gb18030_response.json()["source_kind"] == "text"


def test_extract_copy_input_uses_validated_image_and_ocr(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "productflow_backend.presentation.routes.copy_inputs.extract_text_from_image",
        lambda content: "识别标题\n识别卖点" if content else "",
    )

    response = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("copy.png", _make_demo_image_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert response.json() == {
        "text": "识别标题\n识别卖点",
        "source_kind": "image_ocr",
        "warnings": [],
    }


def test_extract_copy_input_reports_empty_and_unavailable_ocr(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "productflow_backend.presentation.routes.copy_inputs.extract_text_from_image",
        lambda _: "",
    )
    empty = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("empty.png", _make_demo_image_bytes(), "image/png")},
    )
    assert empty.status_code == 400
    assert empty.json() == {"detail": "未识别到可用文字"}

    def raise_unavailable(_: bytes) -> str:
        raise OcrUnavailableError("model unavailable")

    monkeypatch.setattr(
        "productflow_backend.presentation.routes.copy_inputs.extract_text_from_image",
        raise_unavailable,
    )
    unavailable = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("unavailable.png", _make_demo_image_bytes(), "image/png")},
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "OCR 服务暂不可用，请稍后重试"}


def test_extract_copy_input_rejects_invalid_type_size_and_encoding(client: TestClient) -> None:
    unsupported = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("copy.pdf", b"%PDF", "application/pdf")},
    )
    too_large = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("large.txt", b"a" * (2 * 1024 * 1024 + 1), "text/plain")},
    )
    invalid_encoding = client.post(
        "/api/copy-inputs/extract",
        files={"file": ("broken.txt", b"\x81", "text/plain")},
    )

    assert unsupported.status_code == 415
    assert too_large.status_code == 413
    assert invalid_encoding.status_code == 400
    assert invalid_encoding.json() == {"detail": "文案文件编码不支持，请使用 UTF-8 或 GB18030"}


def test_normalize_ocr_result_orders_boxes_by_rows() -> None:
    class FakeOutput:
        txts = ["右侧", "下一行", "左侧"]
        boxes = [
            [[120, 8], [180, 8], [180, 28], [120, 28]],
            [[12, 60], [90, 60], [90, 82], [12, 82]],
            [[10, 10], [80, 10], [80, 30], [10, 30]],
        ]

    assert normalize_ocr_result(FakeOutput()) == "左侧 右侧\n下一行"
