"""OpenAI 客户端按连接参数复用：同参同实例、超时预算变化产生新实例。"""

from __future__ import annotations

from pathlib import Path

import pytest

from productflow_backend.config import get_settings, invalidate_runtime_settings_cache
from productflow_backend.infrastructure.image.images_provider import OpenAIImagesClient
from productflow_backend.infrastructure.image.responses_provider import OpenAIResponsesImageClient
from productflow_backend.infrastructure.provider_config import ResolvedImageProviderConfig

_TINY_PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="


def _images_config(api_key: str = "reuse-key") -> ResolvedImageProviderConfig:
    return ResolvedImageProviderConfig(provider_kind="openai_images", model="gpt-image-2", api_key=api_key)


def _responses_config(api_key: str = "reuse-key") -> ResolvedImageProviderConfig:
    return ResolvedImageProviderConfig(provider_kind="openai_responses", model="gpt-5.4", api_key=api_key)


def test_images_client_reuses_openai_instance_for_same_connection(configured_env: Path) -> None:
    first = OpenAIImagesClient(provider_config=_images_config())._client()
    second = OpenAIImagesClient(provider_config=_images_config())._client()
    assert first is second


def test_images_client_cache_key_separates_api_keys(configured_env: Path) -> None:
    first = OpenAIImagesClient(provider_config=_images_config("reuse-key-a"))._client()
    second = OpenAIImagesClient(provider_config=_images_config("reuse-key-b"))._client()
    assert first is not second


def test_images_client_new_client_when_timeout_budget_changes(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """运行时超时预算变化必须得到带新 timeout 的新客户端（语义不回退）。"""

    first = OpenAIImagesClient(provider_config=_images_config())._client()
    monkeypatch.setenv("IMAGE_GENERATION_PROVIDER_TIMEOUT_SECONDS", "45")
    get_settings.cache_clear()
    invalidate_runtime_settings_cache()
    second = OpenAIImagesClient(provider_config=_images_config())._client()
    assert second is not first


def test_agent_llm_client_reuses_openai_instance_for_same_connection(configured_env: Path) -> None:
    from productflow_backend.application.designer_agent.llm import OpenAICompatAgentClient

    first = OpenAICompatAgentClient(provider_name="t", api_key="reuse-k", base_url="http://x", model="m")
    second = OpenAICompatAgentClient(provider_name="t2", api_key="reuse-k", base_url="http://x", model="m")
    assert first._client is second._client

    other_key = OpenAICompatAgentClient(provider_name="t3", api_key="other-k", base_url="http://x", model="m")
    assert other_key._client is not first._client


def test_responses_client_reuses_openai_instance_across_calls(
    configured_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import productflow_backend.infrastructure.image.responses_provider as responses_provider

    constructions: list[dict] = []

    class DummyImageGenerationCall:
        type = "image_generation_call"
        id = "ig_reuse"
        result = _TINY_PNG_B64

        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, str]:
            return {"id": self.id, "type": self.type, "result": self.result}

    class DummyResponse:
        id = "resp_reuse"
        output = [DummyImageGenerationCall()]

        def model_dump(self, *, mode: str, exclude_none: bool) -> dict[str, object]:
            return {"id": self.id, "output": [self.output[0].model_dump(mode=mode, exclude_none=exclude_none)]}

    class DummyResponses:
        def create(self, **kwargs: object):
            return DummyResponse()

    class DummyOpenAI:
        def __init__(self, **kwargs: object) -> None:
            constructions.append(kwargs)
            self.responses = DummyResponses()

    monkeypatch.setattr(responses_provider, "OpenAI", DummyOpenAI)

    client = OpenAIResponsesImageClient(provider_config=_responses_config())
    first = client.generate_image(prompt="复用一", size="1024x1024")
    second = client.generate_image(prompt="复用二", size="1024x1024")

    assert len(constructions) == 1, "同连接参数的两次调用应复用同一 OpenAI 客户端实例"
    assert first.bytes_data == second.bytes_data
