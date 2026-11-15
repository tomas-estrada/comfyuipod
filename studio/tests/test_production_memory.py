from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from comfy_studio.config import SecretRedactor
from comfy_studio.production_memory import (
    MemoryCapacityError,
    MemoryConflictError,
    MemoryKind,
    MemoryLimits,
    MemoryNotFoundError,
    MemoryValidationError,
    ProductionMemory,
)
from comfy_studio.projects import ProjectStore


def _memory(
    tmp_path, *, limits: MemoryLimits | None = None
) -> tuple[ProjectStore, ProductionMemory, str]:
    store = ProjectStore(tmp_path / "studio")
    project = store.create_project("Project memory", project_id="memory-project")
    return store, ProductionMemory(store, limits=limits or MemoryLimits()), project["id"]


def test_memory_round_trips_through_project_store_and_retrieves_deterministically(
    tmp_path,
) -> None:
    store, memory, project_id = _memory(tmp_path)
    recipe = memory.remember(
        project_id,
        kind=MemoryKind.RECIPE,
        key="nightly-product-reel",
        content={"summary": "Warm amber key light with a slow product push-in"},
        tags=["lighting", "product"],
        source_refs=["asset:video-1", "brief:version-2"],
    )
    preference = memory.remember(
        project_id,
        kind=MemoryKind.PREFERENCE,
        key="lighting",
        content={"summary": "Prefer warm light"},
        tags=["lighting"],
    )

    reopened = ProductionMemory(ProjectStore(store.data_dir))
    results = reopened.retrieve(project_id, "warm amber product lighting")

    assert [item["memory_id"] for item in results] == [
        recipe["memory_id"],
        preference["memory_id"],
    ]
    assert results[0]["kind"] == "recipe"
    assert results[0]["source_refs"] == ["asset:video-1", "brief:version-2"]
    assert store.get_meta(project_id, "production_memory_v1")["schema"] == (
        "comfy_studio.production_memory.v1"
    )


def test_correction_preserves_version_history_and_duplicate_write_is_idempotent(
    tmp_path,
) -> None:
    _, memory, project_id = _memory(tmp_path)
    original = memory.remember(
        project_id,
        kind="decision",
        key="music-rights",
        content={"summary": "Use only cleared library tracks"},
        source_refs=["brief:2"],
    )
    unchanged = memory.remember(
        project_id,
        kind="decision",
        key="music-rights",
        content={"summary": "Use only cleared library tracks"},
        source_refs=["brief:2"],
    )
    corrected = memory.correct(
        project_id,
        original["memory_id"],
        content={"summary": "Use original audio only"},
        reason="Rights review changed the decision",
        source_refs=["brief:3"],
    )
    repeated = memory.correct(
        project_id,
        original["memory_id"],
        content={"summary": "Use original audio only"},
        reason="Rights review changed the decision",
        source_refs=["brief:3"],
    )

    history = memory.get_history(project_id, original["memory_id"])
    assert unchanged["version"] == 1
    assert corrected["version"] == 2
    assert repeated["version"] == 2
    assert [row["version"] for row in history] == [1, 2]
    assert history[0]["content"]["summary"] == "Use only cleared library tracks"
    assert history[1]["correction_reason"] == "Rights review changed the decision"


def test_conflicting_write_requires_explicit_correction(tmp_path) -> None:
    _, memory, project_id = _memory(tmp_path)
    memory.remember(project_id, kind="scene", key="scene-1", content={"summary": "Blue hour"})

    with pytest.raises(MemoryConflictError, match="correct"):
        memory.remember(
            project_id,
            kind="scene",
            key="scene-1",
            content={"summary": "Golden hour"},
        )


def test_project_scoping_and_explicit_delete_remove_memory_and_history(tmp_path) -> None:
    store, memory, first_id = _memory(tmp_path)
    second_id = store.create_project("Other project", project_id="other-project")["id"]
    item = memory.remember(
        first_id, kind="preference", key="captions", content={"summary": "Prefer captions"}
    )

    assert memory.retrieve(second_id, "captions") == []
    assert memory.delete(first_id, item["memory_id"]) is True
    with pytest.raises(MemoryNotFoundError):
        memory.get_history(first_id, item["memory_id"])
    assert memory.delete(first_id, item["memory_id"]) is False


def test_project_deletion_cascades_memory_meta(tmp_path) -> None:
    store, memory, project_id = _memory(tmp_path)
    memory.remember(
        project_id, kind="decision", key="review", content={"summary": "Human approval required"}
    )

    assert store.delete_project(project_id) is True
    assert store.get_meta(project_id, "production_memory_v1") is None


def test_retention_prunes_old_versions_but_keeps_current_version(tmp_path) -> None:
    _, memory, project_id = _memory(tmp_path)
    old_time = datetime(2026, 1, 1, tzinfo=UTC)
    item = memory.remember(
        project_id,
        kind="recipe",
        key="cut",
        content={"summary": "Version one"},
        now=old_time,
    )
    memory.correct(
        project_id,
        item["memory_id"],
        content={"summary": "Version two"},
        reason="More accurate timing",
        now=old_time + timedelta(days=1),
    )

    removed = memory.prune(project_id, older_than=old_time + timedelta(days=2))

    assert removed == 1
    assert [row["version"] for row in memory.get_history(project_id, item["memory_id"])] == [2]


def test_per_memory_history_is_bounded_by_retention_limit(tmp_path) -> None:
    _, memory, project_id = _memory(
        tmp_path, limits=MemoryLimits(max_versions_per_record=2)
    )
    item = memory.remember(
        project_id, kind="scene", key="scene", content={"summary": "Version 1"}
    )
    for version in range(2, 5):
        memory.correct(
            project_id,
            item["memory_id"],
            content={"summary": f"Version {version}"},
            reason=f"Correction {version}",
        )

    history = memory.get_history(project_id, item["memory_id"])

    assert [row["version"] for row in history] == [3, 4]


def test_export_redacts_secret_values_in_content_and_references(tmp_path) -> None:
    _, memory, project_id = _memory(tmp_path)
    memory.remember(
        project_id,
        kind="decision",
        key="credential-note",
        content={"summary": "token sk-test-secret should not leave the project"},
        source_refs=["authorization: Bearer sk-test-secret"],
    )

    exported = memory.export(
        project_id, redactor=SecretRedactor(extra_secrets=["sk-test-secret"])
    )
    serialized = str(exported)

    assert "sk-test-secret" not in serialized
    assert "[REDACTED]" in serialized
    assert exported["schema"] == "comfy_studio.production_memory.v1"


def test_memory_limits_reject_unbounded_records_and_content(tmp_path) -> None:
    _, memory, project_id = _memory(
        tmp_path, limits=MemoryLimits(max_records=1, max_content_bytes=64)
    )
    memory.remember(project_id, kind="scene", key="one", content={"summary": "first"})

    with pytest.raises(MemoryCapacityError, match="max_records"):
        memory.remember(project_id, kind="scene", key="two", content={"summary": "second"})
    with pytest.raises(MemoryValidationError, match="max_content_bytes"):
        memory.correct(
            project_id,
            memory.list_records(project_id)[0]["memory_id"],
            content={"summary": "x" * 100},
            reason="too long",
        )


def test_unknown_memory_id_is_typed(tmp_path) -> None:
    _, memory, project_id = _memory(tmp_path)

    with pytest.raises(MemoryNotFoundError, match="not found"):
        memory.get_history(project_id, "missing")
