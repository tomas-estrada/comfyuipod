from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from comfy_studio.backup import create_backup, verify_backup
from comfy_studio.perception.store import PerceptionVectorStore


def test_backup_snapshots_live_wal_db_and_project_assets(tmp_path: Path) -> None:
    data_dir = tmp_path / "studio data"
    data_dir.mkdir()
    db_path = data_dir / "studio.db"
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE notes (value TEXT)")
    connection.execute("INSERT INTO notes VALUES ('committed in WAL')")
    connection.commit()
    PerceptionVectorStore(data_dir)
    # Keep the writer open; SQLite online backup must include WAL-backed records.
    projects = data_dir / "projects" / "project-1" / "uploads"
    projects.mkdir(parents=True)
    media = projects / "source file.mp4"
    media.write_bytes(b"sample-media-bytes")

    output = tmp_path / "portable.zip"
    try:
        summary = create_backup(data_dir, output)
    finally:
        connection.close()

    assert summary["files"] == 3
    assert verify_backup(output)["valid"] is True
    with zipfile.ZipFile(output) as archive:
        assert archive.read("projects/project-1/uploads/source file.mp4") == b"sample-media-bytes"
        assert (
            archive.getinfo("projects/project-1/uploads/source file.mp4").compress_type
            == zipfile.ZIP_STORED
        )
        assert "index.db" in archive.namelist()
        snapshot_path = tmp_path / "restored-studio.db"
        snapshot_path.write_bytes(archive.read("studio.db"))
    with sqlite3.connect(snapshot_path) as snapshot:
        assert snapshot.execute("SELECT value FROM notes").fetchone() == ("committed in WAL",)


def test_backup_refuses_output_inside_data_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "studio"
    data_dir.mkdir()
    with pytest.raises(ValueError, match="outside STUDIO_DATA_DIR"):
        create_backup(data_dir, data_dir / "backup.zip")


def test_verify_detects_tampered_member(tmp_path: Path) -> None:
    data_dir = tmp_path / "studio"
    (data_dir / "projects" / "p1").mkdir(parents=True)
    source = data_dir / "projects" / "p1" / "asset.bin"
    source.write_bytes(b"original")
    output = tmp_path / "backup.zip"
    create_backup(data_dir, output)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(output) as original, zipfile.ZipFile(tampered, "w") as changed:
        for name in original.namelist():
            content = original.read(name)
            if name == "projects/p1/asset.bin":
                content = b"tampered"
            changed.writestr(name, content)

    with pytest.raises(ValueError, match="integrity mismatch"):
        verify_backup(tampered)


def test_verify_rejects_unsafe_manifest_paths(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.zip"
    manifest = {
        "format": "comfy-studio-backup",
        "format_version": 1,
        "contents": [{"path": "../escape", "size": 1, "sha256": "x"}],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("../escape", b"x")

    with pytest.raises(ValueError, match="Unsafe backup member path"):
        verify_backup(archive_path)
