"""数据隔离的共享判定（批次 B）：把"谁能看这一行"收敛到一处。

隔离语义（与 BATCH_B_PLAN.md 一致）：
- `owner_id IS NULL` 的行是**全局可读**（内置模板等系统资源）；
- `owner_id` 等于当前用户的行走正常流程；
- 其余行对当前用户**视为不存在**——统一抛 NotFoundError，
  与"真的不存在"同文案，避免用错误信息探测他人资源是否存在。

隔离开关闭时不调用本模块的任何判定（调用方以 `user is None` 短路），
因此关闭开关即为整批回退，不需要 revert 代码。
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from sqlalchemy import ColumnElement, or_

from productflow_backend.domain.errors import NotFoundError

T = TypeVar("T")

# 跨用户访问时的统一文案：与"未找到"完全一致，不泄漏存在性
NOT_FOUND_MESSAGE = "不存在"


class OwnedRow(Protocol):
    """任何带 owner_id 的模型行（用于类型提示，运行时不强制）。"""

    owner_id: str | None


def current_owner_id(user: object | None) -> str | None:
    """当前请求的 owner 标识（接受任何带 id 的用户对象）。

    None 表示"遗留模式/隔离关闭"，调用方据此短路——关闭开关即整批回退。
    """
    if user is None:
        return None
    user_id = getattr(user, "id", None)
    return str(user_id) if user_id is not None else None


def owner_filter_expression(column: ColumnElement, owner_id: str | None) -> ColumnElement | None:
    """列表查询的过滤条件：自己的 + 全局可读的（NULL）。

    owner_id 为 None（隔离关闭）时返回 None，调用方据此不加任何条件 = 现状行为。
    """
    if owner_id is None:
        return None
    return or_(column == owner_id, column.is_(None))


def ensure_row_readable(row: OwnedRow, owner_id: str | None, *, message: str = NOT_FOUND_MESSAGE) -> None:
    """单行读/改/删前的归属判定。

    - owner_id 为 None（隔离关闭）→ 放行，保持现状；
    - 行 owner 为空（全局资源）→ 放行；
    - 行 owner 与当前用户一致 → 放行；
    - 否则 → NotFoundError（与未找到同文案）。
    """
    if owner_id is None:
        return
    row_owner = getattr(row, "owner_id", None)
    if row_owner is None or row_owner == owner_id:
        return
    raise NotFoundError(message)


def assign_owner(kwargs: dict, owner_id: str | None) -> dict:
    """创建时的归属写入：隔离开启则写 owner；关闭则不写（保持现状字段为空）。"""
    if owner_id is not None:
        kwargs["owner_id"] = owner_id
    return kwargs
