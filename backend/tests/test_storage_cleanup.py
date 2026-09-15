"""媒体存储生命周期清理测试：孤儿判定、dry_run 语义、真删边界与环境变量覆盖。"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from productflow_backend.infrastructure.db.models import AssetLibraryEntry
from productflow_backend.infrastructure.storage_cleanup import (
    clean_stale_exports,
    collect_referenced_media_paths,
    find_orphan_files,
    run_cleanup,
)

CONTENT = b"x" * 32
OLD_AGE_DAYS = 200


def _make_file(root: Path, relative: str, *, age_days: float | None = None, content: bytes = CONTENT) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if age_days is not None:
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
    return path


def _build_storage_tree(root: Path) -> None:
    """构造一个混合"被引用/孤儿/新生/派生图/导出临时文件"的存储树。"""
    _make_file(root, "products/p1/posters/a.png", age_days=OLD_AGE_DAYS)  # DB 引用
    _make_file(root, "products/p1/posters/.variants/a.preview.webp", age_days=OLD_AGE_DAYS)  # 被引用原图的派生图
    _make_file(root, "products/p2/posters/b.png", age_days=OLD_AGE_DAYS)  # 无引用 → 孤儿
    _make_file(root, "products/p2/posters/.variants/b.thumbnail.webp", age_days=OLD_AGE_DAYS)  # 孤儿派生图
    _make_file(root, "products/p2/posters/young.png")  # 无引用但太新 → 保留
    _make_file(root, "library/brand/c.png", age_days=OLD_AGE_DAYS)  # DB 引用
    _make_file(root, "image_sessions/s1/generated/d.png", age_days=OLD_AGE_DAYS)  # 无引用 → 孤儿
    _make_file(root, "exports/old-grid.zip", age_days=2)  # 过期导出
    _make_file(root, "exports/fresh-grid.zip")  # 新导出


def _media_roots(root: Path) -> list[Path]:
    return [root / "products", root / "image_sessions", root / "library"]


def _orphan_relative_paths(candidates) -> set[str]:
    return {candidate.relative_path for candidate in candidates}


def test_collect_referenced_media_paths_from_db(db_session) -> None:
    db_session.add_all(
        [
            AssetLibraryEntry(kind="reference", source="upload", storage_path="library/brand/c.png"),
            AssetLibraryEntry(kind="template", source="builtin", storage_path="products\\p1\\posters\\a.png"),
        ]
    )
    db_session.commit()

    referenced = collect_referenced_media_paths(db_session)

    assert referenced == {"library/brand/c.png", "products/p1/posters/a.png"}


def test_find_orphan_files_lists_only_unreferenced_expired_files(tmp_path: Path) -> None:
    _build_storage_tree(tmp_path)
    referenced = {"products/p1/posters/a.png", "library/brand/c.png"}
    older_than = datetime.now(UTC) - timedelta(days=180)

    orphans = find_orphan_files(
        _media_roots(tmp_path),
        referenced,
        older_than=older_than,
        base_dir=tmp_path,
    )

    assert _orphan_relative_paths(orphans) == {
        "products/p2/posters/b.png",
        "products/p2/posters/.variants/b.thumbnail.webp",
        "image_sessions/s1/generated/d.png",
    }
    assert all(candidate.size_bytes == len(CONTENT) for candidate in orphans)


def test_find_orphan_files_without_age_filter_includes_recent_files(tmp_path: Path) -> None:
    _build_storage_tree(tmp_path)
    referenced = {"products/p1/posters/a.png", "library/brand/c.png"}

    orphans = find_orphan_files(_media_roots(tmp_path), referenced, base_dir=tmp_path)

    assert "products/p2/posters/young.png" in _orphan_relative_paths(orphans)
    # 被引用的文件任何情况下都不能成为孤儿
    assert "products/p1/posters/a.png" not in _orphan_relative_paths(orphans)
    assert "products/p1/posters/.variants/a.preview.webp" not in _orphan_relative_paths(orphans)
    assert "library/brand/c.png" not in _orphan_relative_paths(orphans)


def test_run_cleanup_dry_run_reports_without_deleting(db_session, tmp_path: Path) -> None:
    _build_storage_tree(tmp_path)
    db_session.add_all(
        [
            AssetLibraryEntry(kind="reference", source="upload", storage_path="library/brand/c.png"),
            AssetLibraryEntry(kind="template", source="builtin", storage_path="products/p1/posters/a.png"),
        ]
    )
    db_session.commit()

    report = run_cleanup(db_session, dry_run=True, storage_root=tmp_path)

    assert report["dry_run"] is True
    assert report["scanned_file_count"] == 7
    assert report["referenced_path_count"] == 2
    assert report["orphan_file_count"] == 3
    assert report["orphan_bytes"] == 3 * len(CONTENT)
    assert report["stale_export_count"] == 1
    assert report["stale_export_bytes"] == len(CONTENT)
    assert report["deleted_file_count"] == 0
    assert report["deleted_bytes"] == 0
    # dry_run 一个文件都不能动
    assert (tmp_path / "products/p2/posters/b.png").exists()
    assert (tmp_path / "exports/old-grid.zip").exists()


def test_run_cleanup_real_run_deletes_only_orphans_and_stale_exports(
    db_session, tmp_path: Path, monkeypatch
) -> None:
    # 物理删除默认被策略闸门拦住，测试需显式授权（与运维开启清理一致的路径）
    monkeypatch.setenv("MEDIA_CLEANUP_ALLOW_DELETE", "1")
    _build_storage_tree(tmp_path)
    db_session.add_all(
        [
            AssetLibraryEntry(kind="reference", source="upload", storage_path="library/brand/c.png"),
            AssetLibraryEntry(kind="template", source="builtin", storage_path="products/p1/posters/a.png"),
        ]
    )
    db_session.commit()

    report = run_cleanup(db_session, dry_run=False, storage_root=tmp_path)

    assert report["deleted_file_count"] == 4
    assert report["deleted_bytes"] == 4 * len(CONTENT)
    assert report["failed_deletions"] == []
    # 孤儿与过期导出被删除
    assert not (tmp_path / "products/p2/posters/b.png").exists()
    assert not (tmp_path / "products/p2/posters/.variants/b.thumbnail.webp").exists()
    assert not (tmp_path / "image_sessions/s1/generated/d.png").exists()
    assert not (tmp_path / "exports/old-grid.zip").exists()
    # 被引用文件、其派生图、新文件全部保留
    assert (tmp_path / "products/p1/posters/a.png").exists()
    assert (tmp_path / "products/p1/posters/.variants/a.preview.webp").exists()
    assert (tmp_path / "products/p2/posters/young.png").exists()
    assert (tmp_path / "library/brand/c.png").exists()
    assert (tmp_path / "exports/fresh-grid.zip").exists()
    # 孤儿清空后空的 .variants 目录被顺手移除
    assert not (tmp_path / "products/p2/posters/.variants").exists()
    assert (tmp_path / "products/p1/posters/.variants").exists()


def test_run_cleanup_never_deletes_referenced_files_even_with_zero_retention(
    db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MEDIA_CLEANUP_ALLOW_DELETE", "1")
    _build_storage_tree(tmp_path)
    db_session.add_all(
        [
            AssetLibraryEntry(kind="reference", source="upload", storage_path="library/brand/c.png"),
            AssetLibraryEntry(kind="template", source="builtin", storage_path="products/p1/posters/a.png"),
        ]
    )
    db_session.commit()
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "0")

    report = run_cleanup(db_session, dry_run=False, storage_root=tmp_path)

    assert (tmp_path / "products/p1/posters/a.png").exists()
    assert (tmp_path / "products/p1/posters/.variants/a.preview.webp").exists()
    assert (tmp_path / "library/brand/c.png").exists()
    # 留存期为 0 时无引用文件全部可删
    assert not (tmp_path / "products/p2/posters/b.png").exists()
    assert not (tmp_path / "products/p2/posters/young.png").exists()
    assert report["media_retention_days"] == 0


def test_retention_env_overrides_boundaries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_RETENTION_DAYS", "7")
    monkeypatch.setenv("EXPORT_TTL_HOURS", "2")
    _make_file(tmp_path, "products/p3/e1.png", age_days=10)
    _make_file(tmp_path, "products/p3/e2.png", age_days=3)
    _make_file(tmp_path, "exports/f1.zip", age_days=0.2)
    _make_file(tmp_path, "exports/f2.zip", age_days=0.01)
    db = _StubDB()

    report = run_cleanup(
        db,
        dry_run=True,
        storage_root=tmp_path,
    )

    assert report["media_retention_days"] == 7
    assert report["export_ttl_hours"] == 2
    assert (tmp_path / "products/p3/e1.png").exists()
    assert (tmp_path / "products/p3/e2.png").exists()
    assert (tmp_path / "exports/f1.zip").exists()
    # MEDIA_RETENTION_DAYS=7：10 天的是孤儿、3 天的不是
    orphans = find_orphan_files(
        [tmp_path / "products"],
        set(),
        older_than=datetime.now(UTC) - timedelta(days=7),
        base_dir=tmp_path,
    )
    assert _orphan_relative_paths(orphans) == {"products/p3/e1.png"}
    # EXPORT_TTL_HOURS=2：0.2 天(4.8h)的过期、0.01 天(14min)的保留
    stale = clean_stale_exports(tmp_path / "exports", older_than=datetime.now(UTC) - timedelta(hours=2))
    assert _orphan_relative_paths(stale) == {"f1.zip"}


def test_clean_stale_exports_skips_missing_dir_and_referenced_paths(tmp_path: Path) -> None:
    assert clean_stale_exports(tmp_path / "exports") == []

    stale_file = _make_file(tmp_path / "exports", "keep.zip", age_days=5)
    fresh_file = _make_file(tmp_path / "exports", "fresh.zip")

    candidates = clean_stale_exports(
        tmp_path / "exports",
        older_than=datetime.now(UTC) - timedelta(hours=24),
        referenced_paths={"keep.zip"},
    )

    assert candidates == []
    assert stale_file.exists() and fresh_file.exists()

    candidates_without_reference = clean_stale_exports(
        tmp_path / "exports",
        older_than=datetime.now(UTC) - timedelta(hours=24),
    )
    assert _orphan_relative_paths(candidates_without_reference) == {"keep.zip"}


class _StubDB:
    """无行的最小 DB 替身：collect_referenced_media_paths 只需要 execute(select(col))。"""

    def execute(self, statement):  # noqa: ANN001, ANN202
        return iter(())


def test_run_cleanup_works_with_empty_reference_set(tmp_path: Path) -> None:
    _make_file(tmp_path, "library/output/solo.png", age_days=OLD_AGE_DAYS)

    report = run_cleanup(_StubDB(), dry_run=True, storage_root=tmp_path)

    assert report["referenced_path_count"] == 0
    assert report["orphan_file_count"] == 1
    assert report["stale_export_count"] == 0


def test_run_cleanup_is_inventory_only_by_default(db_session, tmp_path: Path, monkeypatch) -> None:
    """审计要求：清理默认只盘点。即使调用方传 dry_run=False，未显式授权也不得删文件。"""
    from productflow_backend.infrastructure.storage_cleanup import run_cleanup

    monkeypatch.delenv("MEDIA_CLEANUP_ALLOW_DELETE", raising=False)
    _build_storage_tree(tmp_path)
    orphan = tmp_path / "products" / "p2" / "posters" / "b.png"  # 无引用 → 孤儿
    assert orphan.exists()

    report = run_cleanup(db_session, dry_run=False, storage_root=tmp_path)

    assert report["deletion_blocked_by_policy"] is True
    assert report["dry_run"] is True, "被策略拦下时报告应如实标注为未删除"
    assert report["deleted_file_count"] == 0
    assert orphan.exists(), "未授权的清理必须保留文件"
    assert report["orphan_file_count"] >= 1, "仍应盘点出可清理项供人工审查"
