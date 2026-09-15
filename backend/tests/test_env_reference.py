"""配置文档与拼错检测门禁：让 .env.example 与代码永远一致、让错配不再静默。

背景：Settings 有 68 个字段而 .env.example 曾只手写 40 个（默认值还打架），
且 extra="ignore" 会把拼错的变量静默吞掉。这里用两条测试把它们变成硬约束。
"""

from __future__ import annotations

import re
from pathlib import Path

from productflow_backend.config import Settings, find_suspicious_env_vars

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = REPO_ROOT / ".env.example"


def _documented_keys() -> set[str]:
    """收集 .env.example 里被文档化的变量名（含被注释掉的示例行）。"""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    keys = set()
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            keys.add(key)
    return keys


def test_env_example_documents_every_setting() -> None:
    """.env.example 必须覆盖全部 Settings 字段（新增配置项忘记写文档会在此失败）。"""
    documented = _documented_keys()
    missing = sorted(name.upper() for name in Settings.model_fields if name.upper() not in documented)
    assert not missing, (
        f"这些配置项未在 .env.example 里文档化: {missing}。"
        "请运行 python scripts/gen_env_reference.py 重新生成参考区。"
    )


def test_env_example_generated_section_is_current() -> None:
    """生成区必须是最新的（手改或漏跑生成器都会在此失败）。"""
    from scripts.gen_env_reference import BEGIN_MARKER, END_MARKER, render_reference

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert BEGIN_MARKER in text and END_MARKER in text, "缺少生成区标记"
    current = text[text.index(BEGIN_MARKER): text.index(END_MARKER)]
    expected = render_reference()
    expected_body = expected[: expected.index(END_MARKER)]
    assert current == expected_body, "生成区已过期：请运行 python scripts/gen_env_reference.py"


def test_env_example_has_no_dead_variables() -> None:
    """除已知的非 Settings 变量（compose/系统注入、代码直读）外，不应有死变量。"""
    # compose 注入与代码直读的合法变量
    allowed_extra = {
        "ADMIN_GATE_OPEN_IN_PRODUCTION",
        "APP_HOST",
        "APP_HOST_PORT",
        "AUTH_ENTRY_RATE_LIMIT_PER_MINUTE",
        "DATABASE_URL",
        "MEDIA_CLEANUP_INTERVAL_SECONDS",
        "PIP_INDEX_URL",
        "POSTGRES_DB",
        "POSTGRES_HOST_PORT",
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        "RATE_LIMIT_BACKEND",
        "RECONCILE_INTERVAL_SECONDS",
        "REDIS_HOST_PORT",
        "REMOTE_FETCH_ALLOW_PRIVATE_NETWORK",
        "STORAGE_HOST_PATH",
        "TRUSTED_PROXY_IPS",
        "WEB_PORT",
    }
    known = {name.upper() for name in Settings.model_fields} | allowed_extra
    dead = sorted(key for key in _documented_keys() if key not in known)
    assert not dead, f".env.example 里存在无对应配置的死变量: {dead}"


def test_suspicious_env_var_detection_catches_typos() -> None:
    """拼错的受关注前缀变量必须被识别（否则 extra=ignore 会让用户以为配置生效了）。"""
    environ = {
        "AGENT_MODLE": "grok-4.6",  # 典型拼错
        "IMAGE_GENERATE_MODLE": "x",  # 典型拼错
        "AGENT_MODEL": "grok-4.6",  # 正确，不应报
        "DATABASE_URL": "postgresql://...",  # 无关变量，不应报
        "POSTGRES_PASSWORD": "x",  # compose 注入，不应报
        "ADMIN_GATE_OPEN_IN_PRODUCTION": "1",  # 代码直读的合法变量，不应报
    }
    suspicious = find_suspicious_env_vars(environ)
    assert suspicious == ["AGENT_MODLE", "IMAGE_GENERATE_MODLE"], suspicious


def test_suspicious_env_var_detection_is_quiet_in_normal_setups() -> None:
    """正常配置下不应产生任何告警（窄口径保证零误报，否则告警会被忽略）。"""
    environ = {name.upper(): "x" for name in Settings.model_fields}
    environ.update({"POSTGRES_PASSWORD": "x", "PIP_INDEX_URL": "x", "STORAGE_HOST_PATH": "/data"})
    assert find_suspicious_env_vars(environ) == []


def test_sensitive_defaults_never_appear_in_env_example() -> None:
    """敏感字段的默认值绝不允许出现在 .env.example（含注释）。

    审计指出：原实现虽把变量名注释掉，却仍把默认值打进注释里——今天默认为空
    不代表将来为空，一旦有人给密钥类字段设默认值就会进版本库。
    """
    from productflow_backend.config import Settings

    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    leaked: list[str] = []
    for field_name, field in Settings.model_fields.items():
        if not any(hint in field_name.upper() for hint in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            continue
        default = field.default
        if isinstance(default, str) and len(default) >= 6 and default in text:
            leaked.append(field_name.upper())
    assert not leaked, f"这些敏感字段的默认值出现在 .env.example 里: {leaked}"
