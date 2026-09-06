from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from productflow_backend.config import get_settings, invalidate_runtime_settings_cache
from productflow_backend.infrastructure.db.models import Base
from productflow_backend.infrastructure.db.session import get_engine, get_session_factory


@pytest.fixture(autouse=True)
def _reset_runtime_settings_cache():
    """Runtime settings are cached in-process; start and end every test cold."""

    invalidate_runtime_settings_cache()
    yield
    invalidate_runtime_settings_cache()


@pytest.fixture()
def configured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database_path = tmp_path / "test.db"
    storage_root = tmp_path / "storage"
    monkeypatch.setenv("ADMIN_ACCESS_KEY", "super-secret-admin-key")
    monkeypatch.setenv("SETTINGS_ACCESS_TOKEN", "super-secret-settings-token")
    monkeypatch.setenv("SESSION_SECRET", "super-secret-session-key-123")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("TEXT_PROVIDER_KIND", "mock")
    monkeypatch.setenv("IMAGE_PROVIDER_KIND", "mock")
    monkeypatch.setenv("POSTER_GENERATION_MODE", "template")

    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()

    engine = create_engine(f"sqlite:///{database_path}", future=True, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield storage_root

    Base.metadata.drop_all(engine)
    engine.dispose()
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture()
def db_session(configured_env: Path):
    factory: sessionmaker = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _execute_image_session_queue_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep image-session route tests deterministic while production delivery goes through Dramatiq."""

    from productflow_backend.application.image_sessions import execute_image_session_generation_task

    monkeypatch.setattr(
        "productflow_backend.application.image_sessions.enqueue_image_session_generation_task",
        execute_image_session_generation_task,
    )


class ScriptedAgentLLM:
    """按剧本回放的假 Agent LLM。

    - write_copy 的内部调用消费同一剧本；
    - analyze_template 的视觉调用（content 为多模态列表）返回 vision_response，
      不消耗剧本，便于独立编排。
    """

    provider_name = "scripted"
    model = "scripted-model"

    def __init__(self, script, vision_response: dict | None = None) -> None:
        self.script = list(script)
        self.vision_response = vision_response or {}
        self.calls: list[dict] = []
        self.image_calls = 0

    def chat(self, *, messages, tools):
        from productflow_backend.application.designer_agent.llm import AgentLLMResponse

        self.calls.append({"messages": [dict(item) for item in messages], "tools": tools})
        content = messages[-1].get("content") if messages else None
        if isinstance(content, list):
            self.image_calls += 1
            return AgentLLMResponse(content=json.dumps(self.vision_response, ensure_ascii=False), model=self.model)
        if not self.script:
            return AgentLLMResponse(content="好的。", model=self.model)
        return self.script.pop(0)


@pytest.fixture()
def install_scripted_llm(monkeypatch: pytest.MonkeyPatch):
    """把剧本化假 LLM 注入 designer agent 循环。"""

    def _install(script: list, vision_response: dict | None = None) -> ScriptedAgentLLM:
        client = ScriptedAgentLLM(script, vision_response=vision_response)
        monkeypatch.setattr(
            "productflow_backend.application.designer_agent.loop.build_agent_llm_client",
            lambda: client,
        )
        return client

    return _install
