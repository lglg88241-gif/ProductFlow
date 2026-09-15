"""从 Settings 生成 .env 完整参考，追加到 .env.example 的生成区内。

为什么用生成而不是手写：Settings 有 68 个字段而 .env.example 只手写了 40 个，
手写清单必然持续漂移（这正是审计发现的"~15 个字段无文档 + 默认值打架"）。
生成 + 门禁测试（tests/test_env_reference.py）让文档永远与代码一致。

用法：
    uv run --directory backend python scripts/gen_env_reference.py          # 写入
    uv run --directory backend python scripts/gen_env_reference.py --check  # 只校验是否最新
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BEGIN_MARKER = "# >>> 生成区：由 backend/scripts/gen_env_reference.py 生成，请勿手改 <<<"
END_MARKER = "# >>> 生成区结束 <<<"

# 按前缀分组，让参考可读；未匹配的归入"其他"
GROUP_ORDER = (
    ("AGENT_", "设计师 Agent 供应商（.env 直填时优先生效，中转 API 场景填这里）"),
    ("IMAGE_TOOL_", "生图工具参数（images 接口的 quality/size/背景等）"),
    ("IMAGE_", "图片供应商与生成默认值"),
    ("TEXT_", "文本供应商（旧版遗留，agent 已独立走 AGENT_*）"),
    ("PROMPT_", "提示词模板（通常无需修改）"),
    ("LOG_", "日志与轮转"),
    ("IMAGE_SESSION_", "图片会话与 worker 兜底"),
    ("WORKFLOW_", "商品工作流"),
    ("UPLOAD_", "上传校验"),
    ("POSTER_", "海报渲染"),
    ("SESSION_", "会话 Cookie"),
    ("ADMIN_", "管理员门禁"),
    ("DATABASE_URL/REDIS_URL", "基础设施连接串（compose 会自动注入）"),
)

# 名字里出现这些词视为敏感：只文档化变量名，不写示例值
SENSITIVE_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


def _is_sensitive(field_name: str) -> bool:
    upper = field_name.upper()
    return any(hint in upper for hint in SENSITIVE_HINTS)


def _format_default(field) -> str:
    default = field.default
    if default is None or (isinstance(default, str) and default == ""):
        return "（默认空）"
    if isinstance(default, bool):
        return f"默认 {'true' if default else 'false'}"
    return f"默认 {default}"


def _env_line(name: str, field) -> str:
    if _is_sensitive(name):
        return f"# {name}=                     # {_format_default(field)}；在此填写你的值"
    return f"{name}={field.default if field.default not in (None, '') else ''}"


def _group_for(name: str) -> str:
    # 精确分组优先（IMAGE_TOOL_ 要排在 IMAGE_ 前面）
    for prefix, label in GROUP_ORDER:
        if name.startswith(prefix):
            return label
        if "/" in prefix and name in prefix.split("/"):
            return label
    return "其他"


def render_reference() -> str:
    from productflow_backend.config import Settings

    grouped: dict[str, list[tuple[str, object]]] = {}
    for field_name, field in Settings.model_fields.items():
        env_name = field_name.upper()
        grouped.setdefault(_group_for(env_name), []).append((env_name, field))

    lines = [BEGIN_MARKER, "#", "# 下面是 Settings 的全部环境变量（自动生成，共 %d 项）。" % len(Settings.model_fields),
             "# 未在此显式设置的变量使用括号里标注的默认值。", "#"]
    ordered_labels = [label for _, label in GROUP_ORDER if label in grouped]
    ordered_labels += sorted(label for label in grouped if label not in ordered_labels)

    for label in ordered_labels:
        lines.append(f"# ── {label} ──")
        for env_name, field in grouped[label]:
            lines.append(_env_line(env_name, field))
        lines.append("")
    lines.append(END_MARKER)
    return "\n".join(lines)


def _apply(example_path: Path, generated: str) -> str:
    text = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
    if BEGIN_MARKER in text and END_MARKER in text:
        head = text[: text.index(BEGIN_MARKER)].rstrip()
        tail = text[text.index(END_MARKER) + len(END_MARKER):]
        return f"{head}\n\n{generated}\n{tail.lstrip()}"
    return f"{text.rstrip()}\n\n{generated}\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 .env 完整参考")
    parser.add_argument("--check", action="store_true", help="只校验生成区是否是最新的")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    example_path = repo_root / ".env.example"
    generated = render_reference()

    if args.check:
        current = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        if BEGIN_MARKER not in current or END_MARKER not in current:
            print(".env.example 缺少生成区，请运行 python scripts/gen_env_reference.py")
            sys.exit(1)
        if current[current.index(BEGIN_MARKER): current.index(END_MARKER)] != generated[: generated.index(END_MARKER)]:
            print(".env.example 生成区已过期，请运行 python scripts/gen_env_reference.py")
            sys.exit(1)
        print(".env.example 生成区是最新的")
        return

    example_path.write_text(_apply(example_path, generated), encoding="utf-8")
    print(f"已更新 {example_path}")


if __name__ == "__main__":
    main()
