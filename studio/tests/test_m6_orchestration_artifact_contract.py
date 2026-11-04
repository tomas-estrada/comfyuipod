"""GPU-free tests for truthful production outcomes and final artifact verification."""

from __future__ import annotations

from comfy_studio.orchestration import ProductionCheckpointer, ProductionService
from comfy_studio.orchestration.state import ProductionState


def _state(output_path, *, simulated: bool = False) -> ProductionState:
    return ProductionState(
        idea="test render",
        phase="done",
        final_output_path=str(output_path) if output_path is not None else None,
        metadata={"execution_mode": "simulated" if simulated else "real", "simulated": simulated},
    )


def test_real_run_without_an_output_file_fails_closed(tmp_path) -> None:
    state = _state(tmp_path / "missing.mp4")

    ProductionService._finalize_artifact_status(state)

    assert state.phase == "failed"
    assert state.final_output_path is None
    assert "no produjo" in state.metadata["artifact_error"]
    assert state.log[-1]["kind"] == "artifact_verification_failed"


def test_real_run_requires_ffprobe_verified_video(tmp_path, monkeypatch) -> None:
    from comfy_studio.perception import keyframes

    output = tmp_path / "render.mp4"
    output.write_bytes(b"video-like bytes")
    monkeypatch.setattr(
        keyframes,
        "probe_media_info",
        lambda _path: {"probe_ok": True, "has_video": True},
    )
    valid = _state(output)

    ProductionService._finalize_artifact_status(valid)

    assert valid.phase == "done"
    assert valid.final_output_path == str(output)
    assert valid.metadata["artifact_verified"] is True
    assert valid.log[-1]["kind"] == "artifact_verified"

    monkeypatch.setattr(
        keyframes,
        "probe_media_info",
        lambda _path: {"probe_ok": False, "has_video": True},
    )
    invalid = _state(output)

    ProductionService._finalize_artifact_status(invalid)

    assert invalid.phase == "failed"
    assert invalid.final_output_path is None
    assert "ffprobe" in invalid.metadata["artifact_error"]


def test_stub_run_is_terminal_simulation_without_a_published_path(tmp_path) -> None:
    state = _state("/tmp/stub/fake-reel.mp4", simulated=True)

    ProductionService._finalize_artifact_status(state)

    assert state.phase == "simulated"
    assert state.final_output_path is None
    assert state.metadata["simulated"] is True
    assert state.log[-1]["kind"] == "simulation_complete"


def test_legacy_done_checkpoint_is_migrated_before_status_is_returned(tmp_path) -> None:
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "legacy.sqlite3")
    )
    legacy = ProductionState(
        idea="old stub render",
        phase="done",
        final_output_path="/tmp/stub/old-reel.mp4",
    )
    service.checkpointer.save(legacy)

    status = service.production_status(legacy.id)

    assert status["phase"] == "simulated"
    assert status["final_output_path"] is None
    assert status["simulated"] is True
    assert status["artifact_ready"] is False


def test_simulated_outputs_are_never_registered_in_the_project_gallery(tmp_path) -> None:
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "data")
    project_id = store.create_project(name="simulation")["id"]
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "checkpoints.sqlite3"),
        store=store,
    )
    state = ProductionState(
        idea="test render",
        phase="simulated",
        metadata={"project_id": project_id, "simulated": True},
    )

    assert service._register_outputs_in_gallery(state) == []
    assert state.metadata["output_asset_ids"] == []
    assert store.list_assets(project_id, category="outputs") == []
