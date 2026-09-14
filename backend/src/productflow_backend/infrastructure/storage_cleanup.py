"""媒体存储生命周期清理（保守模式）。

媒体文件（storage/ 下 products/image_sessions/library/.variants）目前只增不减，
本模块提供磁盘侧的孤儿文件盘点与分格导出等临时文件的过期清理入口。

铁律：

- 绝不删除能被任何 DB 行引用到的文件（引用集合来自
  ``SourceAsset/PosterVariant/ImageSessionAsset/AssetLibraryEntry.storage_path``，
  详见 :func:`collect_referenced_media_paths`）；
- 默认 ``dry_run=True``，只出报告不动文件；
- 留存期用模块常量 + 环境变量覆盖（不进 config.py）：
  ``MEDIA_RETENTION_DAYS``（默认 180 天）、``EXPORT_TTL_HOURS``（默认 24 小时）。

孤儿判定方向是"宁可漏删不可误删"：

- 只有"无任何 DB 引用 且 mtime 早于留存期"的文件才列为候选；
- ``.variants`` 派生图按"同目录是否存在同 stem 的 DB 引用"守卫（variant 文件名
  本身永远不会出现在 DB 引用集合里，必须还原成原图 stem 再比对）；
- 符号链接、stat 失败、路径无法归一化的文件一律跳过。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from productflow_backend.config import get_settings
from productflow_backend.infrastructure.db.models import (
    AssetLibraryEntry,
    ImageSessionAsset,
    PosterVariant,
    SourceAsset,
)

DEFAULT_MEDIA_RETENTION_DAYS = 180
DEFAULT_EXPORT_TTL_HOURS = 24
MEDIA_RETENTION_DAYS_ENV = "MEDIA_RETENTION_DAYS"
EXPORT_TTL_HOURS_ENV = "EXPORT_TTL_HOURS"

# 相对 storage root 的媒体根目录；.variants 目录位于其内部，随扫描覆盖
MEDIA_ROOT_NAMES: tuple[str, ...] = ("products", "image_sessions", "library")
EXPORTS_DIR_NAME = "exports"
VARIANT_DIR_NAME = ".variants"
_VARIANT_SUFFIXES: tuple[str, ...] = (".preview", ".thumbnail")


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    """一个可清理的磁盘文件（relative_path 为相对 storage root 的 posix 路径）。"""

    relative_path: str
    absolute_path: Path
    size_bytes: int
    modified_at: datetime


def media_retention_days() -> int:
    """媒体留存天数：环境变量 MEDIA_RETENTION_DAYS 覆盖，默认 180。"""
    return _env_int(MEDIA_RETENTION_DAYS_ENV, DEFAULT_MEDIA_RETENTION_DAYS)


def export_ttl_hours() -> int:
    """分格导出等临时文件留存小时数：EXPORT_TTL_HOURS 覆盖，默认 24。"""
    return _env_int(EXPORT_TTL_HOURS_ENV, DEFAULT_EXPORT_TTL_HOURS)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _normalize_relative_path(value: str) -> str:
    normalized = (value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def _absolute_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def collect_referenced_media_paths(db: Session) -> set[str]:
    """汇集所有 DB 行引用的媒体相对路径（归一化为 posix 相对路径）。

    新增持有 ``storage_path`` 的模型时必须同步加入这里，否则其文件会被误判为孤儿。
    """
    referenced: set[str] = set()
    for column in (
        SourceAsset.storage_path,
        PosterVariant.storage_path,
        ImageSessionAsset.storage_path,
        AssetLibraryEntry.storage_path,
    ):
        for (value,) in db.execute(select(column)):
            normalized = _normalize_relative_path(str(value)) if value is not None else ""
            if normalized:
                referenced.add(normalized)
    return referenced


def _iter_files(roots: Iterable[Path | str]) -> Iterator[Path]:
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for current_dir, dir_names, file_names in os.walk(base, followlinks=False):
            dir_names.sort()
            dir_names[:] = [name for name in dir_names if not (Path(current_dir) / name).is_symlink()]
            for file_name in sorted(file_names):
                file_path = Path(current_dir) / file_name
                if file_path.is_symlink() or not file_path.is_file():
                    continue
                yield file_path


def _path_keys(path: Path, *, scan_root: Path, base_dir: Path | None) -> set[str]:
    """一个文件参与引用匹配的全部键（宁可多匹配，方向是漏删不误删）。"""
    keys: set[str] = {_absolute_key(path)}
    try:
        keys.add(path.relative_to(scan_root).as_posix())
    except ValueError:
        pass
    if base_dir is not None:
        try:
            keys.add(path.relative_to(base_dir).as_posix())
        except ValueError:
            pass
    return keys


def _referenced_key_sets(
    referenced_paths: Iterable[str],
    *,
    base_dir: Path | None,
) -> tuple[set[str], set[str]]:
    """返回 (直接引用键集合, .variants 守卫键集合)。

    守卫键形如 "原图所在目录::原图 stem"：variant 文件永远不会直接出现在
    DB 引用集合里，必须还原成原图 stem 比对，避免误删仍在使用的派生图。
    """
    direct: set[str] = set()
    stem_guard: set[str] = set()
    for reference in referenced_paths:
        normalized = _normalize_relative_path(str(reference))
        if not normalized:
            continue
        direct.add(normalized)
        reference_path = Path(normalized)
        if reference_path.is_absolute():
            direct.add(_absolute_key(reference_path))
        else:
            if base_dir is not None:
                direct.add(_absolute_key(base_dir / reference_path))
            parts = normalized.split("/")
            parent = "/".join(parts[:-1])
            stem_guard.add(f"{parent}::{reference_path.stem}")
    return direct, stem_guard


def _variant_guard_keys(path: Path, *, scan_root: Path, base_dir: Path | None) -> set[str]:
    """若 path 是 .variants 派生图，返回其原图守卫键；否则返回空集合。"""
    if path.parent.name != VARIANT_DIR_NAME:
        return set()
    stem = path.stem
    original_stem: str | None = None
    for suffix in _VARIANT_SUFFIXES:
        if stem.endswith(suffix):
            original_stem = stem[: -len(suffix)]
            break
    if not original_stem:
        return set()
    real_parent = path.parent.parent
    keys: set[str] = set()
    try:
        keys.add(f"{real_parent.relative_to(scan_root).as_posix()}::{original_stem}")
    except ValueError:
        pass
    if base_dir is not None:
        try:
            keys.add(f"{real_parent.relative_to(base_dir).as_posix()}::{original_stem}")
        except ValueError:
            pass
    return keys


def _candidate_from_path(path: Path, *, scan_root: Path, base_dir: Path | None) -> CleanupCandidate | None:
    try:
        stat = path.stat()
    except OSError:
        # stat 失败一律跳过，宁可漏删
        return None
    try:
        relative = path.relative_to(base_dir).as_posix() if base_dir is not None else path.name
    except ValueError:
        relative = path.relative_to(scan_root).as_posix() if scan_root in path.parents else path.name
    return CleanupCandidate(
        relative_path=relative,
        absolute_path=path,
        size_bytes=stat.st_size,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _collect_orphans(
    roots: Iterable[Path | str],
    referenced_paths: Iterable[str],
    *,
    older_than: datetime | None,
    base_dir: Path | None,
) -> tuple[list[CleanupCandidate], int]:
    direct_keys, stem_guard = _referenced_key_sets(referenced_paths, base_dir=base_dir)
    candidates: list[CleanupCandidate] = []
    scanned = 0
    for root in roots:
        scan_root = Path(root)
        for file_path in _iter_files([scan_root]):
            scanned += 1
            keys = _path_keys(file_path, scan_root=scan_root, base_dir=base_dir)
            if keys & direct_keys:
                continue
            if _variant_guard_keys(file_path, scan_root=scan_root, base_dir=base_dir) & stem_guard:
                continue
            candidate = _candidate_from_path(file_path, scan_root=scan_root, base_dir=base_dir)
            if candidate is None:
                continue
            if older_than is not None and candidate.modified_at >= older_than:
                continue
            candidates.append(candidate)
    return candidates, scanned


def find_orphan_files(
    roots: Iterable[Path | str],
    referenced_paths: Iterable[str],
    *,
    older_than: datetime | None = None,
    base_dir: Path | str | None = None,
) -> list[CleanupCandidate]:
    """扫描磁盘文件，剔除 DB 引用集合，只列"无任何引用且早于 older_than"的文件。

    - ``referenced_paths``：DB 中的相对路径集合（见 :func:`collect_referenced_media_paths`），
      同时支持绝对路径形式的引用；
    - ``older_than``：mtime 早于该时刻才算孤儿；``None`` 表示不做留存期过滤；
    - ``base_dir``：DB 相对路径的基准目录（通常为 storage root），用于把引用集合
      换算成绝对键参与匹配。
    """
    resolved_base = Path(base_dir).resolve() if base_dir is not None else None
    candidates, _scanned = _collect_orphans(
        roots,
        referenced_paths,
        older_than=older_than,
        base_dir=resolved_base,
    )
    return candidates


def clean_stale_exports(
    exports_root: Path | str,
    *,
    older_than: datetime | None = None,
    referenced_paths: Iterable[str] | None = None,
) -> list[CleanupCandidate]:
    """盘点分格导出等临时文件（早于 older_than 的全部文件），返回候选列表。

    只盘点不删除（删除统一由 :func:`run_cleanup` 按 dry_run 语义执行）。
    ``referenced_paths`` 提供时，命中的文件一律排除（保险起见，正常情况下
    exports 目录不会出现在 DB 引用集合里）。
    """
    exports_dir = Path(exports_root)
    if not exports_dir.is_dir():
        return []
    referenced: set[str] = set()
    for reference in referenced_paths or ():
        normalized = _normalize_relative_path(str(reference))
        if normalized:
            referenced.add(normalized)
    candidates: list[CleanupCandidate] = []
    for file_path in _iter_files([exports_dir]):
        try:
            relative = file_path.relative_to(exports_dir).as_posix()
        except ValueError:
            continue
        if relative in referenced:
            continue
        try:
            stat = file_path.stat()
        except OSError:
            continue
        candidate = CleanupCandidate(
            relative_path=relative,
            absolute_path=file_path,
            size_bytes=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
        )
        if older_than is not None and candidate.modified_at >= older_than:
            continue
        candidates.append(candidate)
    return candidates


def _remove_empty_variant_dirs(deleted: Iterable[CleanupCandidate]) -> None:
    """删除孤儿后顺手清掉空的 .variants 目录（只删目录本身，失败忽略）。"""
    for candidate in deleted:
        variant_dir = candidate.absolute_path.parent
        if variant_dir.name != VARIANT_DIR_NAME:
            continue
        try:
            variant_dir.rmdir()
        except OSError:
            continue


def run_cleanup(
    db: Session,
    *,
    dry_run: bool = True,
    storage_root: Path | str | None = None,
) -> dict[str, object]:
    """媒体存储清理入口：返回报告 dict（扫描数/可删数/释放字节）。

    默认 ``dry_run=True`` 只出报告；``dry_run=False`` 时执行删除，但每个文件
    删除前仍会对照引用集合做最后一道校验，命中引用一律跳过。
    """
    resolved_root = Path(storage_root or get_settings().storage_root).resolve()
    referenced = collect_referenced_media_paths(db)
    now = datetime.now(UTC)

    media_roots = [resolved_root / name for name in MEDIA_ROOT_NAMES]
    orphans, scanned = _collect_orphans(
        media_roots,
        referenced,
        older_than=now - timedelta(days=max(0, media_retention_days())),
        base_dir=resolved_root,
    )
    stale_exports = clean_stale_exports(
        resolved_root / EXPORTS_DIR_NAME,
        older_than=now - timedelta(hours=max(0, export_ttl_hours())),
        referenced_paths=referenced,
    )
    direct_keys, _stem_guard = _referenced_key_sets(referenced, base_dir=resolved_root)

    deleted_count = 0
    deleted_bytes = 0
    failed_deletions: list[str] = []
    if not dry_run:
        for candidate in (*orphans, *stale_exports):
            # 删除前最后一道校验：任何键命中引用集合立即跳过
            if _normalize_relative_path(candidate.relative_path) in direct_keys:
                continue
            if _absolute_key(candidate.absolute_path) in direct_keys:
                continue
            try:
                candidate.absolute_path.unlink(missing_ok=True)
                deleted_count += 1
                deleted_bytes += candidate.size_bytes
            except OSError:
                failed_deletions.append(candidate.relative_path)
        if deleted_count:
            _remove_empty_variant_dirs(orphans)

    return {
        "dry_run": dry_run,
        "storage_root": str(resolved_root),
        "media_roots": [str(root) for root in media_roots],
        "scanned_file_count": scanned,
        "referenced_path_count": len(referenced),
        "orphan_file_count": len(orphans),
        "orphan_bytes": sum(candidate.size_bytes for candidate in orphans),
        "stale_export_count": len(stale_exports),
        "stale_export_bytes": sum(candidate.size_bytes for candidate in stale_exports),
        "deleted_file_count": deleted_count,
        "deleted_bytes": deleted_bytes,
        "failed_deletions": failed_deletions,
        "media_retention_days": media_retention_days(),
        "export_ttl_hours": export_ttl_hours(),
    }
