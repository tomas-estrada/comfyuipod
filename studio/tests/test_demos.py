"""Banco de evaluación y demostraciones D01–D05 (plan Ultra, W24 / A25).

Harness reproducible con manifest; pasos neurales pendientes de GPU, no done.
Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.demos import (
    Demo,
    DemoError,
    DemoStage,
    EvidenceLevel,
    StageStatus,
    assert_no_fake_gpu_result,
    demo_catalog,
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
