"""Banco de evaluación y demostraciones D01–D05 (plan Ultra, W24 / A25).

Harness reproducible con manifest; pasos neurales pendientes de GPU, no done.
Puro CPU.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

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


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="real FFmpeg/ffprobe binaries required")
def test_d05_runner_encodes_and_probes_exact_4k_fixture(tmp_path: Path) -> None:
    result = run_demo("D05", tmp_path)
    artifact = result["artifacts"][0]
    assert artifact["bytes"] > 0
    assert artifact["sha256"]
    assert result["checks"]["ffmpeg_encode"]["dimensions_verified"] is True
    assert Path(result["manifest"]).parent.joinpath("editable_project.json").is_file()
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["checks"]["ffmpeg_encode"]["quality_claim"] is False


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


def test_cleanup_refuses_symlinked_demo_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "keep.txt"
    protected.write_text("keep", encoding="utf-8")
    output_root = tmp_path / "runs"
    output_root.mkdir()
    (output_root / "D01").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DemoError, match="symlink"):
        clean_demo_output("D01", output_root)
    assert protected.read_text(encoding="utf-8") == "keep"
