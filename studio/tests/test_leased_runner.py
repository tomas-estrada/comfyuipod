"""Integración lease(W03) ↔ executor(fase 14): LeasedEngineRunner.

El executor ejecuta capas a través de un EngineRunner; LeasedEngineRunner
administra el heavy lease de GPU alrededor de cada capa pesada. Verificable en
CPU: no ejecuta GPU, solo administra el lock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from comfy_studio.orchestration.executor import execute_next_shot
from comfy_studio.orchestration.gpu_lease import AdmissionBudget, GpuLease
from comfy_studio.orchestration.leased_runner import LeaseCleanupError, LeasedEngineRunner
from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
    ShotStatus,
)

BUDGET = AdmissionBudget(vram_mb=96 * 1024, ram_mb=256 * 1024)


def _lease(tmp_path: Path) -> GpuLease:
    return GpuLease(lock_dir=tmp_path / ".gpu-lease", budget=BUDGET)


class _RecordingRunner:
    """Runner que registra si el lease estaba tomado cuando corrió la capa."""

    def __init__(self, lease: GpuLease) -> None:
        self._lease = lease
        self.calls: list[dict[str, Any]] = []

    def run_layer(self, engine, job, *, cost_cap=None):  # noqa: ANN001
        held = self._lease.current() is not None
        self.calls.append({"engine": engine, "lease_held": held})
        return {"ok": True, "engine": engine, "output_path": f"/out/{engine}.mp4"}


def test_heavy_layer_holds_lease_during_run_and_releases_after(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    inner = _RecordingRunner(lease)
    runner = LeasedEngineRunner(inner, lease)

    result = runner.run_layer("comfyui", {"kind": "gen"})
    assert result["ok"] is True
    assert result["lease"] == "released"
    # Durante la ejecución, el lease estaba tomado.
    assert inner.calls[0]["lease_held"] is True
    # Tras ejecutar, el lease está libre.
    assert lease.current() is None


def test_recipe_vram_estimate_is_admitted_against_detected_budget(tmp_path: Path) -> None:
    lease = GpuLease(
        lock_dir=tmp_path / ".gpu-lease",
        budget=AdmissionBudget(vram_mb=30_000, ram_mb=64_000),
    )
    inner = _RecordingRunner(lease)
    runner = LeasedEngineRunner(inner, lease)

    result = runner.run_layer("comfyui", {"kind": "gen", "lease_vram_mb": 35_000})

    assert result["ok"] is False
    assert result["lease"] == "error"
    assert "VRAM pedida 35000 MB" in result["error"]
    assert inner.calls == []
    assert lease.current() is None


def test_cpu_layer_does_not_take_the_lease(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    inner = _RecordingRunner(lease)
    runner = LeasedEngineRunner(inner, lease)

    result = runner.run_layer("editor", {"kind": "assemble"})
    assert result["ok"] is True
    # Una capa de CPU no toma el lock de GPU.
    assert inner.calls[0]["lease_held"] is False
    assert "lease" not in result  # no gestionó lease para CPU


def test_cpu_layer_obeys_ram_budget_without_taking_gpu_lease(tmp_path: Path) -> None:
    lease = GpuLease(
        lock_dir=tmp_path / ".gpu-lease",
        budget=AdmissionBudget(vram_mb=30_000, ram_mb=16_000),
    )
    inner = _RecordingRunner(lease)
    runner = LeasedEngineRunner(inner, lease)

    result = runner.run_layer("editor", {"kind": "assemble", "lease_ram_mb": 20_000})

    assert result["ok"] is False
    assert result["lease"] == "error"
    assert inner.calls == []
    assert lease.current() is None


def test_lease_released_even_if_inner_raises(tmp_path: Path) -> None:
    lease = _lease(tmp_path)

    class _Boom:
        def run_layer(self, engine, job, *, cost_cap=None):  # noqa: ANN001
            raise RuntimeError("render reventó")

    runner = LeasedEngineRunner(_Boom(), lease)
    try:
        runner.run_layer("comfyui", {"kind": "gen"})
    except RuntimeError:
        pass
    # Pese a la excepción, el lease no quedó bloqueado.
    assert lease.current() is None


def test_engine_exception_surfaces_cleanup_failure(tmp_path: Path) -> None:
    lease = _lease(tmp_path)

    class _Boom:
        def run_layer(self, engine, job, *, cost_cap=None):  # noqa: ANN001
            raise RuntimeError("render reventó")

    runner = LeasedEngineRunner(_Boom(), lease, unload=lambda: False)
    try:
        runner.run_layer("comfyui", {"kind": "gen"})
    except LeaseCleanupError as exc:
        assert isinstance(exc.__cause__, RuntimeError)
    else:
        raise AssertionError("cleanup failure was hidden by the engine exception")
    assert lease.current() is not None  # uncertain GPU state blocks later engines
    assert lease.release(lease.current(), unload=lambda: True)["released"] is True


def test_busy_gpu_returns_typed_failure_not_overwrite(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    # Otro owner VIVO tiene la GPU.
    import os

    holder = GpuLease(lock_dir=tmp_path / ".gpu-lease", budget=BUDGET, pid=os.getpid())
    holder.acquire(owner="otro", engine="blender", vram_mb=1000, ram_mb=1000)

    runner = LeasedEngineRunner(_RecordingRunner(lease), lease)
    result = runner.run_layer("comfyui", {"kind": "gen"})
    assert result["ok"] is False
    assert result["lease"] == "unavailable"
    assert "ocupada" in result["error"]


def test_unload_runs_on_release(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    calls = {"n": 0}

    def _unload() -> bool:
        calls["n"] += 1
        return True

    runner = LeasedEngineRunner(_RecordingRunner(lease), lease, unload=_unload)
    runner.run_layer("comfyui", {"kind": "gen"})
    assert calls["n"] == 1


def test_unload_failure_cannot_report_success(tmp_path: Path) -> None:
    lease = _lease(tmp_path)
    runner = LeasedEngineRunner(
        _RecordingRunner(lease), lease, unload=lambda: False
    )

    result = runner.run_layer("comfyui", {"kind": "gen"})

    assert result["ok"] is False
    assert result["lease"] == "cleanup_failed"
    assert result["lease_cleanup_failed"] is True
    assert "cleanup failed" in result["error"]
    # Uncertain GPU state remains quarantined until cleanup is verified.
    grant = lease.current()
    assert grant is not None
    assert lease.release(grant, unload=lambda: True)["released"] is True
    assert lease.current() is None


def test_unload_exception_cannot_report_success(tmp_path: Path) -> None:
    lease = _lease(tmp_path)

    def _unload() -> bool:
        raise OSError("device reset failed")

    runner = LeasedEngineRunner(_RecordingRunner(lease), lease, unload=_unload)
    result = runner.run_layer("comfyui", {"kind": "gen"})

    assert result["ok"] is False
    assert result["lease"] == "cleanup_failed"
    assert lease.current() is not None
    assert lease.release(lease.current(), unload=lambda: True)["released"] is True


def test_executor_end_to_end_with_leased_runner(tmp_path: Path) -> None:
    """El executor real corre un shot a través del LeasedEngineRunner."""
    lease = _lease(tmp_path)
    inner = _RecordingRunner(lease)
    runner = LeasedEngineRunner(inner, lease)

    shot = Shot(description="dron costero", id="shot_1")
    shot.layers = [Layer(kind="gen", engine="comfyui", template_id="wan_t2v")]
    state = ProductionState(idea="reel")
    state.sequences = [Sequence(title="s1", id="seq_1", shots=[shot])]

    out = execute_next_shot(state, runner)
    ran = out.shot_by_id("shot_1")
    assert ran.status == ShotStatus.EVALUATING  # ejecutado, listo para evaluar
    assert ran.output_path == "/out/comfyui.mp4"
    # El lease quedó libre tras el shot.
    assert lease.current() is None
    # La capa pesada corrió con el lease tomado.
    assert inner.calls[0]["lease_held"] is True
