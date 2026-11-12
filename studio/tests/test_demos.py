"""Banco de evaluación y demostraciones D01–D05 (plan Ultra, W24 / A25).

Harness reproducible con manifest; pasos neurales pendientes de GPU, no done.
Puro CPU.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from comfy_studio import demos as demos_module
from comfy_studio.demos import (
    Demo,
    DemoError,
    DemoStage,
    EvidenceLevel,
    StageStatus,
    assert_no_fake_gpu_result,
    clean_demo_output,
    demo_catalog,
    run_demo,
)


def test_catalog_has_five_demos_with_manifests() -> None:
    demos = demo_catalog()
    assert {d.demo_id for d in demos} == {"D01", "D02", "D03", "D04", "D05"}
    for d in demos:
        d.validate()  # no lanza
        assert d.inputs and d.expected and d.splits
        assert d.budget_runs > 0


def test_neural_stages_are_pending_gpu_not_done() -> None:
    """A25: los renders futuros no se marcan como resultados existentes."""
    for d in demo_catalog():
        for s in d.stages:
            if s.neural:
                assert s.status is StageStatus.PENDING_GPU
                assert s.status is not StageStatus.DONE
                assert s.pending_reason


def test_cpu_stages_are_ready() -> None:
    d01 = next(d for d in demo_catalog() if d.demo_id == "D01")
    cpu = d01.cpu_ready()
    assert cpu  # hay pasos CPU listos (reconstruccion_guias, foley)
    assert all(s.status is StageStatus.READY_CPU for s in cpu)


def test_assert_no_fake_gpu_result_catches_disguised_render() -> None:
    """A25: un paso neural done sin evidencia GPU se rechaza."""
    d = Demo(
        demo_id="DX",
        title="t",
        inputs=["i"],
        expected=["e"],
        splits=["s"],
        budget_runs=1,
        stages=[
            DemoStage(name="gen", level=EvidenceLevel.L2, status=StageStatus.DONE, neural=True),
        ],
    )
    with pytest.raises(DemoError, match="resultado existente"):
        assert_no_fake_gpu_result(d)


def test_neural_stage_done_requires_l3() -> None:
    bad = DemoStage(name="g", level=EvidenceLevel.L2, status=StageStatus.DONE, neural=True)
    with pytest.raises(DemoError, match="sin evidencia GPU"):
        bad.validate()
    # Con L3, un paso neural done es válido (evidencia GPU real).
    good = DemoStage(name="g", level=EvidenceLevel.L3, status=StageStatus.DONE, neural=True)
    good.validate()  # no lanza


def test_demo_without_manifest_rejected() -> None:
    d = Demo(demo_id="DX", title="t", inputs=[], expected=[], splits=[], budget_runs=1)
    with pytest.raises(DemoError, match="sin inputs"):
        d.validate()


def test_demo_serialization_reports_cpu_and_gpu_counts() -> None:
    d01 = next(d for d in demo_catalog() if d.demo_id == "D01")
    data = d01.to_dict()
    assert data["gpu_pending"] >= 1
    assert data["cpu_ready"] >= 1
    assert len(data["stages"]) == 5


@pytest.mark.parametrize("demo_id", ["D01", "D02", "D03", "D04"])
def test_cpu_demo_runner_writes_hashed_fixture_evidence(tmp_path: Path, demo_id: str) -> None:
    result = run_demo(demo_id, tmp_path)
    assert result["result"] == "cpu_fixture_checks_passed"
    manifest_path = Path(result["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["inputs"][0]["sha256"]
    assert manifest["claim_scope"].startswith("fixture consistency")
    assert all(stage["status"] == ("pending_gpu" if stage["neural"] else "done")
               for stage in manifest["stages"])
    assert all(stage["pending_reason"] for stage in manifest["stages"] if stage["neural"])


def test_d01_manifest_binds_verified_atlas_references_and_states_camera_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(repo_root / "atlas"))

    result = run_demo("D01", tmp_path)
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    evidence = manifest["checks"]["atlas_reference_evidence"]
    atlas_inputs = [item for item in manifest["inputs"] if item.get("kind") == "atlas_reference"]

    d01 = next(demo for demo in demo_catalog() if demo.demo_id == "D01")
    assert all(name in manifest["checks"] for name in d01.expected)
    assert evidence["place_id"] == "plc_medellin_avenida_el_poblado"
    assert evidence["reference_count"] == 3
    assert evidence["gps_coordinate_span_m"] == pytest.approx(86.03, abs=0.2)
    assert evidence["verified_camera_view_count"] == 0
    assert evidence["estimated_camera_view_count"] == 0
    assert evidence["camera_coverage_status"] == "not_established"
    assert len(atlas_inputs) == 3
    assert all(item["sha256_verified"] is True for item in atlas_inputs)
    assert all(item["license"] == "CC BY 3.0" for item in atlas_inputs)
    assert all(item["license_url"] and item["attribution"] for item in atlas_inputs)


def test_d01_resume_rechecks_atlas_files_before_reusing_a_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    source_root = repo_root / "atlas"
    fixture = json.loads(
        (repo_root / "studio/comfy_studio/data/demos/d01_vehicle_cameras.json").read_text()
    )
    atlas_root = tmp_path / "atlas"
    files_root = atlas_root / "files"
    files_root.mkdir(parents=True)
    shutil.copy2(source_root / "catalog.db", atlas_root / "catalog.db")

    catalog_uri = f"{(source_root / 'catalog.db').as_uri()}?mode=ro"
    source_connection = sqlite3.connect(catalog_uri, uri=True)
    try:
        file_paths = [
            source_connection.execute(
                """SELECT r.file_path FROM views v
                   JOIN atlas_refs r ON r.ref_id=v.ref_id WHERE v.view_id=?""",
                (view_id,),
            ).fetchone()[0]
            for view_id in fixture["atlas_site"]["view_ids"]
        ]
    finally:
        source_connection.close()
    for relative in file_paths:
        target = files_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / "files" / relative, target)

    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    run = run_demo("D01", tmp_path / "runs")
    manifest_path = Path(run["manifest"])
    first_asset = files_root / file_paths[0]
    first_asset.write_bytes(first_asset.read_bytes() + b"tampered")

    with pytest.raises(DemoError, match="SHA-256 del Atlas no coincide"):
        run_demo("D01", tmp_path / "runs", resume=True)
    assert manifest_path.is_file()


def test_d01_catalog_open_errors_are_reported_as_demo_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(repo_root / "atlas"))

    def fail_catalog_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        assert "?mode=ro&immutable=1" in args[0]
        raise sqlite3.OperationalError("read-only catalog unavailable")

    monkeypatch.setattr(demos_module.sqlite3, "connect", fail_catalog_open)
    with pytest.raises(DemoError, match="no se pudo leer el catálogo Atlas: OperationalError"):
        run_demo("D01", tmp_path)


def test_d01_resume_rebuilds_manifest_missing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(repo_root / "atlas"))

    first = run_demo("D01", tmp_path)
    manifest_path = Path(first["manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["result"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    resumed = run_demo("D01", tmp_path, resume=True)

    assert resumed["result"] == "cpu_fixture_checks_passed"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["result"] == (
        "cpu_fixture_checks_passed"
    )


def test_pending_gpu_stage_requires_explanation() -> None:
    stage = DemoStage(name="render", level=EvidenceLevel.L2,
                      status=StageStatus.PENDING_GPU, neural=True)
    with pytest.raises(DemoError, match="motivo explícito"):
        stage.validate()


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="real FFmpeg/ffprobe binaries required",
)
def test_d05_runner_encodes_and_probes_exact_4k_fixture(tmp_path: Path) -> None:
    result = run_demo("D05", tmp_path)
    artifacts = {artifact["path"]: artifact for artifact in result["artifacts"]}
    assert "D05_delivery_package.zip" in artifacts
    assert "D05_asset_inventory.json" in artifacts
    assert artifacts["D05_delivery_package.zip"]["bytes"] > 0
    assert artifacts["D05_delivery_package.zip"]["sha256"]
    assert result["checks"]["asset_inventory"]["input_count"] == 100
    assert result["checks"]["asset_inventory"]["corrupt"] == 5
    assert result["checks"]["asset_inventory"]["unsupported"] == 5
    assert result["checks"]["asset_inventory"]["pending_weights"] == 90
    assert result["checks"]["asset_inventory"]["duplicate"] == 0
    assert result["checks"]["budget"]["within_budget"] is True
    assert result["checks"]["brief_to_edl"]["cuts"] >= 1
    assert result["checks"]["editable_roundtrip"]["title_edit_preserved_clips"] is True
    assert result["checks"]["editable_roundtrip"]["credits_verified"] is True
    assert result["checks"]["ffmpeg_encode"]["dimensions_verified"] == {
        "16:9": True,
        "9:16": True,
    }
    assert result["checks"]["ffmpeg_encode"]["fps_verified"] == {
        "16:9": True,
        "9:16": True,
    }
    assert result["checks"]["ffmpeg_encode"]["color_range_verified"] == {
        "16:9": True,
        "9:16": True,
    }
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["checks"]["ffmpeg_encode"]["quality_claim"] is False
    assert manifest["checkpoints"][-1]["status"] == "done"
    inventory = json.loads(
        Path(result["manifest"]).parent.joinpath("D05_asset_inventory.json").read_text()
    )
    assert len(inventory["inputs"]) == 100
    assert all(row["sha256"] for row in inventory["inputs"] if row["status"] in {
        "pending_weights", "duplicate"
    })
    measured_audio_durations = {
        row["duration_sec"]
        for row in inventory["inputs"]
        if row["kind"] == "audio" and row["duration_sec"] is not None
    }
    assert len(measured_audio_durations) >= 10
    measured_video_durations = {
        row["duration_sec"]
        for row in inventory["inputs"]
        if row["kind"] == "video" and row["duration_sec"] is not None
    }
    assert len(measured_video_durations) >= 10
    resumed = run_demo("D05", tmp_path, resume=True)
    assert resumed["resumed"] is True


def test_runner_rejects_unknown_demo(tmp_path: Path) -> None:
    with pytest.raises(DemoError, match="desconocida"):
        run_demo("D99", tmp_path)


def test_resume_reuses_verified_run_and_cleanup_is_scoped(tmp_path: Path) -> None:
    first = run_demo("D01", tmp_path)
    resumed = run_demo("D01", tmp_path, resume=True)
    assert resumed["resumed"] is True
    assert resumed["manifest"] == first["manifest"]
    assert clean_demo_output("D01", tmp_path) is True
    assert clean_demo_output("D01", tmp_path) is False
    assert not Path(first["manifest"]).exists()


def test_cleanup_refuses_symlinked_demo_directory(tmp_path: Path, symlink_or_skip) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    output_root = tmp_path / "runs"
    output_root.mkdir()
    symlink_or_skip(output_root / "D01", outside)
    with pytest.raises(DemoError, match="symlink"):
        clean_demo_output("D01", output_root)
    assert protected.read_text(encoding="utf-8") == "keep"


def test_runner_refuses_symlinked_demo_directory(
    tmp_path: Path, symlink_or_skip
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    output_root = tmp_path / "runs"
    output_root.mkdir()
    symlink_or_skip(output_root / "D01", outside)
    with pytest.raises(DemoError, match="output symlink"):
        run_demo("D01", output_root)
    assert list(outside.iterdir()) == []


def test_runner_refuses_symlinked_manifest_and_d05_artifacts(
    tmp_path: Path, symlink_or_skip
) -> None:
    outside = tmp_path / "protected.json"
    outside.write_text("protected", encoding="utf-8")
    output_root = tmp_path / "runs"
    (output_root / "D01").mkdir(parents=True)
    symlink_or_skip(output_root / "D01" / "run_manifest.json", outside, directory=False)
    with pytest.raises(DemoError, match="output symlink"):
        run_demo("D01", output_root)
    assert outside.read_text(encoding="utf-8") == "protected"

    (output_root / "D05").mkdir()
    symlink_or_skip(output_root / "D05" / "editable_project.json", outside, directory=False)
    with pytest.raises(DemoError, match="output symlink"):
        run_demo("D05", output_root)
    assert outside.read_text(encoding="utf-8") == "protected"

