"""供应商用途分类：设计师 Agent 独立绑定（生图专用与 Agent 大脑分离）+ 主备降级链。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import _login, _unlock_settings

from productflow_backend.application.designer_agent.llm import (
    AgentLLMError,
    AgentLLMResponse,
    FallbackAgentLLMClient,
)
from productflow_backend.infrastructure.provider_config import resolve_agent_provider_config


class FlakyLLM:
    def __init__(self, *, name: str, fail: bool, model: str) -> None:
        self.provider_name = name
        self.model = model
        self.fail = fail
        self.calls = 0

    def chat(self, *, messages, tools, intent: str = "") -> AgentLLMResponse:
        self.calls += 1
        if self.fail:
            raise AgentLLMError(f"{self.provider_name} 不可用")
        return AgentLLMResponse(content=f"from-{self.provider_name}", model=self.model)


def _create_text_profile(client: TestClient, *, name: str, base_url: str) -> str:
    created = client.post(
        "/api/settings/provider-profiles",
        json={
            "name": name,
            "provider_type": "openai_compatible",
            "base_url": base_url,
            "api_key": f"key-{name}",
            "capabilities": ["text_responses"],
            "default_models": {},
            "config": {},
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _binding_payload(profile_id: str, *, fallback_profile_id: str | None = None) -> dict:
    payload = {
        "provider_kind": "openai",
        "provider_profile_id": profile_id,
        "model_settings": {"model": "grok-4.6"},
        "config": {},
    }
    if fallback_profile_id:
        payload["model_settings"]["fallback_model"] = "gemini-3.8-flash"
        payload["config"]["fallback_profile_id"] = fallback_profile_id
    return payload


@pytest.fixture()
def unlocked_client(configured_env: Path) -> TestClient:
    from productflow_backend.presentation.api import create_app

    client = TestClient(create_app())
    _login(client)
    _unlock_settings(client)
    return client


def test_agent_binding_is_independent_from_text_binding(unlocked_client: TestClient) -> None:
    xai_profile = _create_text_profile(unlocked_client, name="xAI", base_url="https://api.x.ai/v1")
    updated = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json=_binding_payload(xai_profile),
    )
    assert updated.status_code == 200, updated.text
    binding = updated.json()
    assert binding["purpose"] == "agent"
    assert binding["model_settings"]["model"] == "grok-4.6"

    # text 绑定保持 mock 不受影响；agent 绑定独立可用
    text_binding = unlocked_client.get("/api/settings/provider-config").json()["bindings"]
    by_purpose = {item["purpose"]: item for item in text_binding}
    assert by_purpose["text"]["provider_kind"] == "mock"
    assert by_purpose["agent"]["provider_kind"] == "openai"

    resolve_agent_provider_config.cache_clear if hasattr(resolve_agent_provider_config, "cache_clear") else None
    resolved = resolve_agent_provider_config()
    assert resolved.provider_kind == "openai"
    assert resolved.model == "grok-4.6"
    assert resolved.base_url == "https://api.x.ai/v1"
    assert resolved.fallback_provider_profile_id is None

    from productflow_backend.application.designer_agent.loop import is_agent_llm_available

    available, _ = is_agent_llm_available()
    assert available is True, "agent 绑定独立于 text mock，应当可用"


def test_agent_binding_with_fallback_chain(unlocked_client: TestClient) -> None:
    xai_profile = _create_text_profile(unlocked_client, name="xAI-FB", base_url="https://api.x.ai/v1")
    gemini_profile = _create_text_profile(
        unlocked_client, name="Gemini", base_url="https://generativelanguage.googleapis.com/v1beta/openai"
    )
    updated = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json=_binding_payload(xai_profile, fallback_profile_id=gemini_profile),
    )
    assert updated.status_code == 200, updated.text

    resolved = resolve_agent_provider_config()
    assert resolved.model == "grok-4.6"
    assert resolved.fallback_provider_profile_id == gemini_profile
    assert resolved.fallback_model == "gemini-3.8-flash"
    assert resolved.fallback_base_url == "https://generativelanguage.googleapis.com/v1beta/openai"

    # 降级行为：主供应商失败 → 备用接管
    primary = FlakyLLM(name="grok", fail=True, model="grok-4.6")
    fallback = FlakyLLM(name="gemini", fail=False, model="gemini-3.8-flash")
    chain = FallbackAgentLLMClient(primary=primary, fallback=fallback)
    response = chain.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert response.content == "from-gemini"
    assert primary.calls == 1 and fallback.calls == 1

    # 主供应商正常时不触发降级
    healthy = FlakyLLM(name="grok", fail=False, model="grok-4.6")
    chain2 = FallbackAgentLLMClient(primary=healthy, fallback=fallback)
    chain2.chat(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert fallback.calls == 1  # 未增加


def test_agent_binding_validation_errors(unlocked_client: TestClient) -> None:
    xai_profile = _create_text_profile(unlocked_client, name="xAI-V", base_url="https://api.x.ai/v1")

    missing_model = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json={"provider_kind": "openai", "provider_profile_id": xai_profile, "model_settings": {}, "config": {}},
    )
    assert missing_model.status_code == 400
    assert "模型未配置" in missing_model.json()["detail"]

    # 中转 API 场景：主备同一档案（同站换模型）合法
    relay_fallback = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json={
            "provider_kind": "openai",
            "provider_profile_id": xai_profile,
            "model_settings": {"model": "grok-4.6", "fallback_model": "gemini-3.8-flash"},
            "config": {"fallback_profile_id": xai_profile},
        },
    )
    assert relay_fallback.status_code == 200, relay_fallback.text
    assert relay_fallback.json()["config"]["fallback_profile_id"] == xai_profile

    unknown_fallback = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json={
            "provider_kind": "openai",
            "provider_profile_id": xai_profile,
            "model_settings": {"model": "grok-4.6", "fallback_model": "gemini-3.8-flash"},
            "config": {"fallback_profile_id": "no-such-profile"},
        },
    )
    assert unknown_fallback.status_code == 400
    assert "降级供应商档案不存在" in unknown_fallback.json()["detail"]


def test_agent_purpose_survives_settings_export_import(configured_env: Path) -> None:
    from productflow_backend.presentation.api import create_app

    app = create_app()
    client = TestClient(app)
    _login(client)
    _unlock_settings(client)

    profile_id = _create_text_profile(client, name="xAI-EXP", base_url="https://api.x.ai/v1")
    updated = client.patch(
        "/api/settings/provider-bindings/agent",
        json=_binding_payload(profile_id),
    )
    assert updated.status_code == 200

    exported = client.get("/api/settings/export").json()
    agent_bindings = [item for item in exported["provider_bindings"] if item["purpose"] == "agent"]
    assert agent_bindings and agent_bindings[0]["provider_kind"] == "openai"

    # 导入同一份文件（含 agent 用途）应通过校验
    imported = client.post("/api/settings/import", json=exported)
    assert imported.status_code == 200, imported.text
    bindings = {item["purpose"]: item for item in imported.json()["provider_config"]["bindings"]}
    assert bindings["agent"]["provider_kind"] == "openai"



def test_agent_config_falls_back_to_env_variables(configured_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """绑定保持 mock 时，.env 的 AGENT_* 直读生效（用户自提供 API 场景）。"""
    monkeypatch.setenv("AGENT_PROVIDER_KIND", "openai")
    monkeypatch.setenv("AGENT_API_KEY", "relay-key")
    monkeypatch.setenv("AGENT_BASE_URL", "https://relay.example.com/v1")
    monkeypatch.setenv("AGENT_MODEL", "grok-4.6")
    monkeypatch.setenv("AGENT_FALLBACK_API_KEY", "relay-key")
    monkeypatch.setenv("AGENT_FALLBACK_BASE_URL", "https://relay.example.com/v1")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("AGENT_FALLBACK_MODEL", "gemini-3.8-flash")
    from productflow_backend.config import get_settings, invalidate_runtime_settings_cache

    get_settings.cache_clear()
    invalidate_runtime_settings_cache()

    resolved = resolve_agent_provider_config()
    assert resolved.provider_kind == "openai"
    assert resolved.model == "grok-4.6"
    assert resolved.api_key == "relay-key"
    assert resolved.base_url == "https://relay.example.com/v1"
    assert resolved.fallback_model == "gemini-3.8-flash"
    assert resolved.fallback_api_key == "relay-key"


def test_ui_binding_overrides_env_variables(
    configured_env: Path,
    unlocked_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """界面上配置的 agent 绑定优先于 .env（其余照常）。"""
    monkeypatch.setenv("AGENT_PROVIDER_KIND", "openai")
    monkeypatch.setenv("AGENT_API_KEY", "env-key")
    monkeypatch.setenv("AGENT_BASE_URL", "https://env.example.com/v1")
    from productflow_backend.config import get_settings, invalidate_runtime_settings_cache

    get_settings.cache_clear()
    invalidate_runtime_settings_cache()

    profile_id = _create_text_profile(unlocked_client, name="UI 档案", base_url="https://ui.example.com/v1")
    updated = unlocked_client.patch(
        "/api/settings/provider-bindings/agent",
        json=_binding_payload(profile_id),
    )
    assert updated.status_code == 200

    resolved = resolve_agent_provider_config()
    assert resolved.api_key is None or resolved.base_url != "https://env.example.com/v1"
    assert resolved.base_url == "https://ui.example.com/v1"
    assert resolved.provider_profile_id == profile_id
