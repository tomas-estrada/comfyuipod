"""Regressión CPU-only: el executor NUNCA reusa ni ensambla artefactos colgantes.

Tras un corte a mitad de shot, el checkpoint guarda `layer.output_path` de las
capas ya completadas. Si el fichero ya no está en disco (volumen efímero,
limpieza, crash antes del flush), el executor anterior lo reusaba a ciegas y el
reel se ensamblaba con una ruta inexistente. Ahora: el artefacto se valida
(existe + bytes > 0) antes de reusarse; si está podrido se descarta el estado
del intento y la capa se re-ejecuta con identidad de operación fresca
(`replan_attempts` sube → idempotency_key nuevo), SIN consumir escalón de
replan — perder el fichero no es un fallo del plan.
"""

from __future__ import annotations

from pathlib import Path

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.executor import execute_next_shot
from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
    ShotStatus,
)


class ArtifactRunner:
    """Runner determinista: escribe bytes reales y graba cada llamada."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, str]] = []
        self.n = 0

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.n += 1
        key = str(job["idempotency_key"])
        self.calls.append((engine, key))
        out = self.tmp_path / f"{engine}_{self.n}.out"
        out.write_bytes(b"artifact bytes\n")
        return {"ok": True, "engine": engine, "output_path": str(out)}


class InterruptingRunner(ArtifactRunner):
    """Falla el motor-b (corte de proceso) tras completar engine-a."""

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        if engine == "engine-b":
            raise RuntimeError("simulated process interruption")
        return super().run_layer(engine, job, cost_cap=cost_cap, cancel_event=cancel_event)


def _two_layer_state() -> tuple[ProductionState, Shot]:
    shot = Shot(
        description="two layer render",
        layers=[Layer(kind="gen", engine="engine-a"), Layer(kind="upscale", engine="engine-b")],
    )
    state = ProductionState(idea="artefactos vivos tras reinicio")
    state.sequences = [Sequence(title="one", shots=[shot])]
    return state, shot


def _interrupt_first_attempt(
    state: ProductionState, tmp_path: Path, checkpointer: ProductionCheckpointer
) -> tuple[ProductionState, ArtifactRunner]:
    first = InterruptingRunner(tmp_path)
    try:
        execute_next_shot(state, first, checkpoint=checkpointer.save)
    except RuntimeError as exc:
        assert "interruption" in str(exc)
    interrupted = checkpointer.load(state.id)
    assert interrupted is not None
    assert interrupted.all_shots()[0].status == ShotStatus.RUNNING
    return interrupted, first


def test_restart_reuses_checkpointed_artifact_that_exists(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "reuse.sqlite3")
    state, _shot = _two_layer_state()
    interrupted, _first = _interrupt_first_attempt(state, tmp_path, checkpointer)
    assert interrupted.all_shots()[0].layers[0].output_path is not None

    runner = ArtifactRunner(tmp_path)
    resumed = execute_next_shot(interrupted, runner, checkpoint=checkpointer.save)

    # la capa con artefacto VIVO se reusa: engine-a NO se re-ejecuta.
    assert [engine for engine, _ in runner.calls] == ["engine-b"]
    assert resumed.all_shots()[0].status == ShotStatus.EVALUATING
    assert "stale_artifact" not in [e["kind"] for e in resumed.log]


def test_restart_with_deleted_artifact_retries_fresh_identity(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "stale.sqlite3")
    state, _shot = _two_layer_state()
    interrupted, first = _interrupt_first_attempt(state, tmp_path, checkpointer)

    stale_path = interrupted.all_shots()[0].layers[0].output_path
    stale_key = dict(first.calls)["engine-a"]
    assert stale_path is not None
    # el pod perdió el artefacto entre corte y reanudación:
    Path(stale_path).unlink()

    runner = ArtifactRunner(tmp_path)
    resumed = execute_next_shot(interrupted, runner, checkpoint=checkpointer.save)

    shot = resumed.all_shots()[0]
    # la capa con artefacto PODRIDO se re-ejecuta, con identidad de op fresca.
    assert [engine for engine, _ in runner.calls] == ["engine-a", "engine-b"]
    fresh_key = dict(runner.calls)["engine-a"]
    assert fresh_key != stale_key
    events = [e["kind"] for e in resumed.log]
    assert "stale_artifact" in events
    assert "layer_transient_retry" not in events
    # no consumió escalón de replan: perder el fichero no es un fallo del plan.
    assert shot.replan_tier is None
    assert shot.replan_attempts == 1
    assert shot.status == ShotStatus.EVALUATING
    # el artefacto nuevo existe y es el que quedó registrado en el shot.
    new_path = shot.layers[0].output_path
    assert new_path is not None
    assert Path(new_path).is_file() and Path(new_path).stat().st_size > 0


def test_zero_byte_artifact_is_rejected_as_stale(tmp_path):
    shot = Shot(description="x", layers=[Layer(kind="gen", engine="engine-a")])
    state = ProductionState(idea="artefacto vacío")
    state.sequences = [Sequence(title="s", shots=[shot])]
    out = tmp_path / "empty.out"
    out.write_bytes(b"")  # existe pero sin bytes: podrido igual que ausente
    shot.layers[0].output_path = str(out)
    shot.status = ShotStatus.RUNNING

    runner = ArtifactRunner(tmp_path)
    resumed = execute_next_shot(state, runner)

    assert "stale_artifact" in [e["kind"] for e in resumed.log]
    assert [engine for engine, _ in runner.calls] == ["engine-a"]
    assert resumed.all_shots()[0].status == ShotStatus.EVALUATING
    assert resumed.all_shots()[0].layers[0].output_path != str(out)
