"""生图链路超时预算测试：provider HTTP 超时、后台轮询 deadline 与配置默认值。"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import pytest

from productflow_backend.config import get_runtime_settings, get_settings
from productflow_backend.infrastructure.image import responses_provider
from productflow_backend.infrastructure.image.images_provider import OpenAIImagesClient
from productflow_backend.infrastructure.image.responses_provider import (
    PROVIDER_BACKGROUND_POLL_TIMEOUT_MESSAGE,
    OpenAIResponsesImageClient,
    image_provider_http_timeout,
)
from productflow_backend.infrastructure.provider_config import ResolvedImageProviderConfig


def _images_provider_config() -> ResolvedImageProviderConfig:
    return ResolvedImageProviderConfig(
        provider_kind="openai_images",
        model="gpt-image-2",
        api_key="demo-api-key",
    )


def _responses_provider_config() -> ResolvedImageProviderConfig:
    return ResolvedImageProviderConfig(
        provider_kind="openai_responses",
        model="gpt-5.4",
        api_key="demo-api-key",
    )


def test_image_generation_provider_timeout_defaults(configured_env: Path) -> None:
    settings = get_settings()
    assert settings.image_generation_provider_timeout_seconds == 300
    assert get_runtime_settings().image_generation_provider_timeout_seconds == 300


def test_worker_failsafe_time_limit_default_lowered_to_one_hour(configured_env: Path) -> None:
    settings = get_settings()
    assert settings.image_session_worker_failsafe_time_limit_minutes == 60


def test_image_generation_provider_timeout_env_override(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER_TIMEOUT_SECONDS", "45")
    get_settings.cache_clear()

    assert get_settings().image_generation_provider_timeout_seconds == 45
    assert get_runtime_settings().image_generation_provider_timeout_seconds == 45
    timeout = image_provider_http_timeout()
    assert timeout.connect == 10.0
    assert timeout.read == 45.0
    assert timeout.write == 45.0
    assert timeout.pool == 45.0


def test_image_provider_http_timeout_enforces_minimum() -> None:
    timeout = image_provider_http_timeout(0.1)
    assert timeout.connect == 10.0
    assert timeout.read == 1.0


def test_images_api_client_uses_provider_http_timeout(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER_TIMEOUT_SECONDS", "45")
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class DummyOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("productflow_backend.infrastructure.image.images_provider.OpenAI", DummyOpenAI)

    client = OpenAIImagesClient(provider_config=_images_provider_config())
    client._client()

    assert isinstance(captured["timeout"], httpx.Timeout)
    assert captured["timeout"].connect == 10.0
    assert captured["timeout"].read == 45.0
    assert captured["timeout"].write == 45.0
    assert captured["timeout"].pool == 45.0


def test_responses_client_timeout_kwarg(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER_TIMEOUT_SECONDS", "45")
    get_settings.cache_clear()
    captured: dict[str, Any] = {}

    class DummyImageGenerationCall:
        type = "image_generation_call"
        id = "ig_timeout"
        result = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )

        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, str]:
            return {"id": self.id, "type": self.type, "result": self.result}

    class DummyResponse:
        id = "resp_timeout"
        output = [DummyImageGenerationCall()]

        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, Any]:
            return {"id": self.id, "output": [self.output[0].model_dump(mode=mode, exclude_none=exclude_none)]}

    class DummyResponses:
        def create(self, **kwargs: Any):
            return DummyResponse()

    class DummyOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.responses = DummyResponses()

    monkeypatch.setattr(responses_provider, "OpenAI", DummyOpenAI)

    client = OpenAIResponsesImageClient(provider_config=_responses_provider_config())
    assert client.provider_timeout_seconds == 45.0
    client.generate_image(prompt="超时预算", size="1024x1024")

    assert isinstance(captured.get("timeout"), httpx.Timeout)
    assert captured["timeout"].read == 45.0
    assert captured["timeout"].connect == 10.0


def test_poll_background_response_deadline_exceeded_raises_identifiable_error(
    configured_env: Path,
) -> None:
    client = OpenAIResponsesImageClient(provider_config=_responses_provider_config())

    class InProgressResponse:
        id = "resp_stuck"
        status = "in_progress"

    class StuckResponses:
        def retrieve(self, response_id: str) -> InProgressResponse:
            return InProgressResponse()

    class StuckClient:
        responses = StuckResponses()

    with pytest.raises(RuntimeError, match="图片供应商后台生成超时"):
        client._poll_background_response(
            StuckClient(),
            InProgressResponse(),
            request_payload={"model": "gpt-5.4"},
            progress_callback=None,
            task_context={},
            deadline=monotonic() - 0.01,
        )


def test_generate_image_propagates_poll_timeout_message(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = OpenAIResponsesImageClient(provider_config=_responses_provider_config())

    class QueuedResponse:
        id = "resp_queued"
        status = "queued"

    class DummyResponses:
        def create(self, **kwargs: Any):
            return QueuedResponse()

    class DummyOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            self.responses = DummyResponses()

    monkeypatch.setattr(responses_provider, "OpenAI", DummyOpenAI)

    def failing_poll(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(PROVIDER_BACKGROUND_POLL_TIMEOUT_MESSAGE)

    monkeypatch.setattr(client, "_poll_background_response", failing_poll)

    with pytest.raises(RuntimeError) as exc_info:
        client.generate_image(prompt="后台超时", size="1024x1024")

    assert str(exc_info.value) == PROVIDER_BACKGROUND_POLL_TIMEOUT_MESSAGE
