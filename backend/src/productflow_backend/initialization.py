"""运维初始化 CLI（批次 B 第一批，纯新增）。

用法::

    python -m productflow_backend.initialization create-admin --username X --password Y

行为：
- 创建 ``role=admin`` 的首个管理员账号；
- 已存在任意管理员时明确报错并以退出码 1 结束（不做"再建一个"）；
- 密码长度不足 8 位拒绝并以退出码 1 结束；
- 依赖与 Web 服务相同的环境变量（DATABASE_URL 等，见 config.Settings）。
"""

from __future__ import annotations

import argparse
import sys

from productflow_backend.application.user_accounts import (
    MIN_PASSWORD_LENGTH,
    InvalidUserDataError,
    UserAccountsError,
    UsernameTakenError,
    create_user,
    has_any_admin,
)
from productflow_backend.infrastructure.db.session import get_session_factory

CLI_ERROR_EXIT_CODE = 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m productflow_backend.initialization",
        description="ProductFlow 运维初始化工具（创建首个管理员等）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_admin = subparsers.add_parser("create-admin", help="创建首个管理员账号（role=admin）")
    create_admin.add_argument("--username", required=True, help="管理员用户名（1-64 字符）")
    create_admin.add_argument("--password", required=True, help=f"管理员密码（至少 {MIN_PASSWORD_LENGTH} 位）")
    create_admin.add_argument("--display-name", default=None, help="可选的显示名称")

    return parser


def _create_admin(args: argparse.Namespace) -> int:
    if len(args.password or "") < MIN_PASSWORD_LENGTH:
        print(f"错误：密码长度至少 {MIN_PASSWORD_LENGTH} 位", file=sys.stderr)
        return CLI_ERROR_EXIT_CODE

    session = get_session_factory()()
    try:
        if has_any_admin(session):
            print("错误：已存在管理员账号，不再重复创建（如需更多管理员请走邀请流程）", file=sys.stderr)
            return CLI_ERROR_EXIT_CODE
        user = create_user(
            session,
            username=args.username,
            password=args.password,
            role="admin",
            display_name=args.display_name,
        )
    except UsernameTakenError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return CLI_ERROR_EXIT_CODE
    except InvalidUserDataError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return CLI_ERROR_EXIT_CODE
    finally:
        session.close()

    print(f"管理员创建成功：{user.username}（role=admin）")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 入口；返回进程退出码。"""
    args = _build_parser().parse_args(argv)
    if args.command == "create-admin":
        try:
            return _create_admin(args)
        except UserAccountsError as exc:  # 服务层未细分错误的兜底
            print(f"错误：{exc}", file=sys.stderr)
            return CLI_ERROR_EXIT_CODE
    # argparse 已强制子命令合法，这里只是防御
    print(f"错误：未知命令 {args.command}", file=sys.stderr)
    return CLI_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
