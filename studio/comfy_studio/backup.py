"""Portable, integrity-checked snapshots of Studio metadata and project assets.

Usage::

    python -m comfy_studio.backup create /path/to/studio-backup.zip
    python -m comfy_studio.backup verify /path/to/studio-backup.zip

The SQLite online-backup API is used so a live WAL database is never copied as a
raw file. Model weights and application configuration are deliberately excluded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .config import _default_studio_data_dir

_FORMAT = "comfy-studio-backup"
_FORMAT_VERSION = 1
_DB_FILES = ("studio.db", "index.db")
_PROJECTS_DIR = "projects"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Take a transactionally consistent SQLite snapshot, including live WAL pages."""
    src = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(destination, timeout=30)
    try:
        src.backup(dst, pages=256, sleep=0.05)
        result = dst.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {source.name}: {result}")
    finally:
        dst.close()
        src.close()


def _iter_project_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise ValueError(f"Projects root is a symlink; refusing unsafe snapshot: {root}")
    if not root.exists():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Project data contains a symlink; refusing unsafe snapshot: {path}")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())


def create_backup(data_dir: Path | str, output: Path | str) -> dict[str, Any]:
    """Create an atomic ZIP snapshot and return its manifest summary."""
    source_root = Path(data_dir).resolve()
    destination = Path(output).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"Studio data directory not found: {source_root}")
    if destination == source_root or source_root in destination.parents:
        raise ValueError("Backup output must be outside STUDIO_DATA_DIR")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="comfy_studio_backup_") as temp_name:
        staging = Path(temp_name)
        staged_files: list[tuple[str, Path]] = []
        for db_name in _DB_FILES:
            live_db = source_root / db_name
            if live_db.is_file():
                snap = staging / db_name
                _sqlite_snapshot(live_db, snap)
                staged_files.append((db_name, snap))
        project_root = source_root / _PROJECTS_DIR
        for path in _iter_project_files(project_root):
            archive_name = PurePosixPath(
                _PROJECTS_DIR, *path.relative_to(project_root).parts
            ).as_posix()
            staged_files.append((archive_name, path))

        records = [
            {"path": name, "size": path.stat().st_size, "sha256": _sha256(path)}
            for name, path in staged_files
        ]
        manifest = {
            "format": _FORMAT,
            "format_version": _FORMAT_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "contents": records,
            "excluded": ["model weights", "application configuration", "secrets"],
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        tmp_archive = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                tmp_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                archive.writestr("manifest.json", manifest_bytes)
                for name, path in staged_files:
                    # Video/audio/image assets are normally already compressed;
                    # storing them avoids expensive recompression on large projects.
                    compression = zipfile.ZIP_DEFLATED if name in _DB_FILES else zipfile.ZIP_STORED
                    archive.write(path, arcname=name, compress_type=compression)
            # Verify the completed archive before publishing it atomically.
            verify_backup(tmp_archive)
            os.replace(tmp_archive, destination)
        finally:
            tmp_archive.unlink(missing_ok=True)

    return {"path": str(destination), "files": len(records), "bytes": destination.stat().st_size}


def verify_backup(archive_path: Path | str) -> dict[str, Any]:
    """Validate archive structure, manifest paths, byte lengths, and SHA-256 hashes."""
    path = Path(archive_path).expanduser().resolve()
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Backup archive contains duplicate paths")
        if "manifest.json" not in names:
            raise ValueError("Backup manifest.json is missing")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != _FORMAT or manifest.get("format_version") != _FORMAT_VERSION:
            raise ValueError("Unsupported Studio backup format/version")
        listed: set[str] = set()
        for record in manifest.get("contents", []):
            name = record.get("path")
            if not isinstance(name, str) or not name or "\\" in name:
                raise ValueError("Backup manifest contains an invalid path")
            member_path = PurePosixPath(name)
            if member_path.is_absolute() or any(
                part in {"", ".", ".."} for part in member_path.parts
            ):
                raise ValueError(f"Unsafe backup member path: {name!r}")
            if name == "manifest.json" or name not in names:
                raise ValueError(f"Backup member is missing or reserved: {name!r}")
            if name in listed:
                raise ValueError(f"Duplicate manifest path: {name!r}")
            listed.add(name)
            digest_obj = hashlib.sha256()
            byte_count = 0
            with archive.open(name, "r") as member:
                for block in iter(lambda: member.read(1024 * 1024), b""):
                    byte_count += len(block)
                    digest_obj.update(block)
            if byte_count != int(record.get("size", -1)) or digest_obj.hexdigest() != record.get(
                "sha256"
            ):
                raise ValueError(f"Backup integrity mismatch: {name}")
        extras = set(names) - listed - {"manifest.json"}
        if extras:
            raise ValueError(f"Unlisted files in backup: {sorted(extras)}")
    return {"valid": True, "files": len(listed), "created_at": manifest.get("created_at")}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create or verify portable Comfy Studio backups.")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create an atomic, integrity-checked ZIP snapshot")
    create.add_argument(
        "output", type=Path, help="Destination .zip (must be outside STUDIO_DATA_DIR)"
    )
    create.add_argument(
        "--data-dir",
        type=Path,
        default=_default_studio_data_dir(os.environ),
        help="Studio data root (defaults to STUDIO_DATA_DIR / COMFY_DATA_DIR resolution)",
    )
    verify = commands.add_parser(
        "verify", help="Verify manifest and SHA-256 for every archive file"
    )
    verify.add_argument("archive", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "create":
            result = create_backup(args.data_dir, args.output)
        else:
            result = verify_backup(args.archive)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
