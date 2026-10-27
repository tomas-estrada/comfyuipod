"""Tests para SQLite studio.db con schema versionado (proyectos, jobs, assets)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from comfy_studio.projects import SCHEMA_VERSION, ProjectStore


def test_sqlite_studio_db_created_with_versioned_schema(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    db_path = studio_data_dir / "studio.db"
    assert db_path.is_file()
    assert store.get_schema_version() == SCHEMA_VERSION

    with sqlite3.connect(db_path) as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == SCHEMA_VERSION

        versions = conn.execute(
            "SELECT version, description FROM schema_version ORDER BY version"
        ).fetchall()
        assert len(versions) >= 1
        assert versions[-1][0] == SCHEMA_VERSION

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "schema_version" in tables
        assert "projects" in tables
        assert "jobs" in tables
        assert "assets" in tables


def test_sqlite_migrations_are_idempotent(studio_data_dir: Path) -> None:
    store1 = ProjectStore(studio_data_dir)
    proj = store1.create_project(name="Idempotent Check", description="Test")

    # Re-instanciar sobre el mismo directorio no pierde datos ni duplica versión
    store2 = ProjectStore(studio_data_dir)
    assert store2.get_schema_version() == SCHEMA_VERSION
    fetched = store2.get_project(proj["id"])
    assert fetched is not None
    assert fetched["name"] == "Idempotent Check"
