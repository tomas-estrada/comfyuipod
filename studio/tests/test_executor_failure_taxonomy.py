"""Tests CPU-only de la taxonomía tipada de fallos de capa (orchestration.failures).

Contrato:
- TRANSIENT declarado → reintentos in-place ACOTADOS con la MISMA identidad de
  operación (el idempotency_key no cambia: la op nunca completó), sin consumir
  escalón de replan; agotado el presupuesto, escala la escalera como hoy.
- INVALID_PARAMETERS → cero reintentos idénticos; directo a la escalera.
- PERMISSION_DENIED → fail-fast tipado: shot FAILED sin tocar la escalera.
- Resultado legacy (solo `error`, sin clase) → comportamiento EXACTO previo.
"""

from __future__ import annotations

from pathlib import Path

from comfy_studio.orchestration.executor import execute_next_shot
from comfy_studio.orchestration.failures import (
    MAX_TRANSIENT_RETRIES,
    LayerFailureKind,
    classify_runner_failure,
)
from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
    ShotStatus,
)


def _state(engine: str = "engine-a") -> tuple[ProductionState, Shot]:
    shot = Shot(description="x", layers=[Layer(kind="gen", engine=engine)])
    state = ProductionState(idea="taxonomía de fallos")
    state.sequences = [Sequence(title="s", shots=[shot])]
    return state, shot


class TransientRunner:
    """Falla transitoriamente las primeras `fail_times` veces, luego OK."""

    def __init__(self, fail_times: int, tmp_path: Path) -> None:
        self.fail_times = fail_times
        self.tmp_path = tmp_path
        self.keys: list[str] = []

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.keys.append(str(job["idempotency_key"]))
        if len(self.keys) <= self.fail_times:
            return {"ok": False, "error": "gpu ocupada", "failure_kind": "transient"}
        out = self.tmp_path / "out.bin"
        out.write_bytes(b"ok\n")
        return {"ok": True, "output_path": str(out)}


def test_transient_retries_in_place_without_consuming_tier(tmp_path):
    state, shot = _state()
    runner = TransientRunner(fail_times=MAX_TRANSIENT_RETRIES, tmp_path=tmp_path)

    execute_next_shot(state, runner)

    # 1 intento inicial + MAX_TRANSIENT_RETRIES reintentos, todos en la MISMA
    # identidad de operación (la op nunca completó).
    assert len(runner.keys) == MAX_TRANSIENT_RETRIES + 1
    assert len(set(runner.keys)) == 1
    assert shot.status == ShotStatus.EVALUATING
    assert shot.replan_tier is None
    assert shot.replan_attempts == 0
    events = [e["kind"] for e in state.log]
    assert events.count("layer_transient_retry") == MAX_TRANSIENT_RETRIES
    assert "execute_failed" not in events


def test_transient_exhaustion_escalates_ladder(tmp_path):
    state, shot = _state()

    class AlwaysTransient:
        calls = 0

        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            AlwaysTransient.calls += 1
            return {"ok": False, "error": "timeout", "failure_kind": "timeout"}

    runner = AlwaysTransient()
    execute_next_shot(state, runner)

    # presupuesto de reintentos agotado DENTRO de una misma ejecución; el
    # escalón se consume una única vez al agotarse (no por reintento).
    assert AlwaysTransient.calls == MAX_TRANSIENT_RETRIES + 1
    assert shot.status == ShotStatus.REPLANNING
    assert str(shot.replan_tier) == "local"
    kinds = [e["kind"] for e in state.log]
    assert kinds.count("layer_transient_retry") == MAX_TRANSIENT_RETRIES
    assert kinds.count("execute_failed") == 1


def test_transient_retry_receives_only_remaining_cost_cap(tmp_path):
    state, shot = _state()
    shot.cost_cap_usd = 1.0
    caps: list[float | None] = []

    class CostedTransientRunner:
        calls = 0

        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            caps.append(cost_cap)
            CostedTransientRunner.calls += 1
            if CostedTransientRunner.calls == 1:
                return {
                    "ok": False,
                    "error": "retryable timeout",
                    "failure_kind": "transient",
                    "cost_usd": 0.35,
                }
            out = tmp_path / "costed.out"
            out.write_bytes(b"ok")
            return {"ok": True, "output_path": str(out), "cost_usd": 0.1}

    execute_next_shot(state, CostedTransientRunner())

    assert caps == [1.0, 0.65]
    assert shot.cost_spent_usd == 0.45


def test_invalid_parameters_never_retried_in_place(tmp_path):
    state, shot = _state()
    calls: list[int] = []

    class InvalidRunner:
        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            calls.append(1)
            return {
                "ok": False,
                "error": "spec inválido",
                "failure_kind": "invalid_parameters",
            }

    execute_next_shot(state, InvalidRunner())

    assert len(calls) == 1  # sin reintento idéntico
    assert shot.status == ShotStatus.REPLANNING  # directo a la escalera
    assert str(shot.replan_tier) == "local"
    kinds = [e["kind"] for e in state.log]
    assert "layer_transient_retry" not in kinds
    assert kinds.count("execute_failed") == 1
    assert state.log[-1]["failure_kind"] == str(LayerFailureKind.INVALID_PARAMETERS)


def test_permission_denied_fails_shot_typed_without_tier(tmp_path):
    state, shot = _state()
    calls: list[int] = []

    class ForbiddenRunner:
        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            calls.append(1)
            return {
                "ok": False,
                "error": "escritura fuera del proyecto",
                "failure_kind": "permission_denied",
            }

    execute_next_shot(state, ForbiddenRunner())

    assert len(calls) == 1
    assert shot.status == ShotStatus.FAILED
    assert "[permission_denied]" in (shot.error or "")
    # no consumió escalón: el replan no puede arreglar una autorización.
    assert shot.replan_tier is None
    assert shot.replan_attempts == 0
    assert "permission_denied" in [e["kind"] for e in state.log]
    assert "execute_failed" not in [e["kind"] for e in state.log]


def test_legacy_error_only_result_keeps_ladder_behavior(tmp_path):
    state, shot = _state()
    calls: list[int] = []

    class LegacyFailRunner:
        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            calls.append(1)
            return {"ok": False, "error": "boom"}

    execute_next_shot(state, LegacyFailRunner())

    assert len(calls) == 1  # sin reintentos in-place
    assert shot.status == ShotStatus.REPLANNING
    assert str(shot.replan_tier) == "local"
    assert shot.error == "boom"
    kinds = [e["kind"] for e in state.log]
    assert "layer_transient_retry" not in kinds
    assert state.log[-1]["failure_kind"] == str(LayerFailureKind.ENGINE)


def test_lease_unavailable_is_treated_as_transient(tmp_path):
    state, shot = _state()

    class LeaseBusyThenOk:
        def __init__(self) -> None:
            self.calls = 0

        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            self.calls += 1
            if self.calls == 1:
                return {
                    "ok": False,
                    "engine": engine,
                    "error": "GPU ocupada",
                    "lease": "unavailable",
                }
            out = tmp_path / "out.bin"
            out.write_bytes(b"ok\n")
            return {"ok": True, "output_path": str(out)}

    runner = LeaseBusyThenOk()
    resumed = execute_next_shot(state, runner)

    assert runner.calls == 2  # reintento in-place, sin escalón
    assert resumed.all_shots()[0].status == ShotStatus.EVALUATING
    assert shot.replan_tier is None


def test_engine_reason_maps_to_taxonomy():
    # reason tipado de EngineResult (via to_public_dict) sin failure_kind.
    assert (
        classify_runner_failure({"ok": False, "reason": "timeout"})
        is LayerFailureKind.TRANSIENT
    )
    assert (
        classify_runner_failure({"ok": False, "reason": "spec_invalid"})
        is LayerFailureKind.INVALID_PARAMETERS
    )
    # lease del LeasedEngineRunner.
    assert (
        classify_runner_failure({"ok": False, "lease": "unavailable"})
        is LayerFailureKind.TRANSIENT
    )
    # legacy sin señales → ENGINE (comportamiento previo).
    assert classify_runner_failure({"ok": False, "error": "boom"}) is LayerFailureKind.ENGINE
    # clase explícita no reconocida → fail-safe a ENGINE.
    assert (
        classify_runner_failure({"ok": False, "failure_kind": "quantum_fluctuation"})
        is LayerFailureKind.ENGINE
    )
    # la clase explícita gana sobre reason/lease.
    assert (
        classify_runner_failure(
            {"ok": False, "failure_kind": "permission_denied", "lease": "unavailable"}
        )
        is LayerFailureKind.PERMISSION_DENIED
    )
