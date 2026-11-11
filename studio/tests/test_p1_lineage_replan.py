"""Regressiones P1: lineage en el replan, gasto acumulativo y briefs ambiguos.

Tres contratos CPU-only (sin GPU, sin pesos, sin APIs de pago):

1. El DAG de lineage (W04) deja de ser código muerto: el executor registra la
   procedencia de cada shot computado y el lazo de replan (evaluador) invalida
   selectivamente — propio + descendientes, nunca laterales — al escalar
   local→plantilla→global. La invalidación alcanza al ESTADO real: los
   descendientes stale pierden sus outputs, quedan REPLANNING con identidad de
   op nueva (el idempotency_key incluye `replan_attempts`) y se re-ejecutan;
   el ensamblaje usa el output NUEVO, jamás el stale. Los hermanos intactos no
   se re-ejecutan.
2. El gasto (`cost_spent_usd`) es CUMULATIVO entre escalones: sobrevive al
   `escalate_replan` y a los reinicios, y cada intento recibe el cap RESTANTE.
   Alcanzado/superado el cap positivo, el runner NO se invoca (fail-closed: 0
   como cost_cap es ambiguo aguas abajo). Y como los adaptadores de motor
   actuales NO producen `cost_usd`, el estado distingue gasto reportado de
   desconocido (`spend_reported`; `cost_spent_usd=None` si nadie reportó).
3. Un brief de propósito ambiguo NO descarta su hipótesis en silencio: o produce
   el plan conservador COMPLETO (hipótesis preservada) o termina con una razón
   tipada (`ambiguous_purpose`), nunca con un `max_steps` inespecífico.
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration import (
    Collaborators,
    Layer,
    LineageGraph,
    PlanTerminalReason,
    ProductionCheckpointer,
    ProductionService,
    ProductionState,
    ReplanTier,
    Sequence,
    Shot,
    ShotStatus,
    assemble_reel,
    evaluate_and_replan,
    execute_next_shot,
    plan_production,
)
from comfy_studio.orchestration.evaluator import StubShotEvaluator
from comfy_studio.orchestration.executor import record_shot_lineage
from comfy_studio.orchestration.service import ProductionAlreadyTerminalError

# --- helpers ---------------------------------------------------------------


class CostRunner:
    """Runner determinista que reporta gasto por run y graba el cap recibido."""

    def __init__(self, cost_usd: float = 0.4) -> None:
        self.cost_usd = cost_usd
        self.caps: list[float | None] = []

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.caps.append(cost_cap)
        return {
            "ok": True,
            "engine": engine,
            "output_path": f"/tmp/stub/{engine}.out",
            "cost_usd": self.cost_usd,
        }


class FlakyRunner:
    """Primer run OK, el resto falla (modela un corte a mitad de replan)."""

    def __init__(self) -> None:
        self.n = 0

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.n += 1
        if self.n == 1:
            return {"ok": True, "engine": engine, "output_path": "/tmp/first.out"}
        return {"ok": False, "engine": engine, "error": "intento de replan falla"}


class EmptyPlanner:
    """Planner LLM que no determina el propósito: devuelve un plan sin shots."""

    def plan(self, idea, *, context=None):
        return {"sequences": [{"title": "secuencia principal", "shots": []}]}


def _accept(st: ProductionState) -> None:
    """Acepta los shots en EVALUATING con el evaluador stub por defecto."""
    evaluate_and_replan(st, StubShotEvaluator(score=0.9))


# --- 1. lineage: registro del executor --------------------------------------


def test_executor_records_lineage_for_computed_shots():
    a = Shot(description="a", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    st = ProductionState(idea="lineage vivo")
    st.sequences.append(Sequence(title="s", shots=[a, b]))

    execute_next_shot(st)
    _accept(st)
    execute_next_shot(st)

    graph = LineageGraph.from_dict(st.lineage)
    na, nb = graph.get(a.id), graph.get(b.id)
    assert na is not None and nb is not None
    assert na.artifact == a.output_path
    assert nb.artifact == b.output_path
    assert na.fingerprint and nb.fingerprint
    assert nb.depends_on == [a.id]
    # el DAG serializado sobrevive al checkpointer (mismo contrato que el estado).
    restored = ProductionState.from_dict(st.to_dict())
    rgraph = LineageGraph.from_dict(restored.lineage)
    assert rgraph.get(a.id).fingerprint == na.fingerprint
    assert rgraph.get(b.id).artifact == nb.artifact


def test_local_replan_invalidates_own_artifact_but_keeps_fingerprint():
    shot = Shot(
        description="x",
        acceptance={"vbench_min": 0.99},
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    st = ProductionState(idea="replan local")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    execute_next_shot(st)

    fp_before = LineageGraph.from_dict(st.lineage).get(shot.id).fingerprint
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, StubShotEvaluator(score=0.1))  # escalón LOCAL

    assert shot.replan_tier == ReplanTier.LOCAL
    node = LineageGraph.from_dict(st.lineage).get(shot.id)
    # el artifact propio se descarta (el reintento lo regenera)...
    assert node.artifact is None
    # ...pero las entradas no cambiaron: mismo fingerprint, sin invalidación
    # lateral (A05: reintentar no es cambiar la entrada).
    assert node.fingerprint == fp_before


def test_template_replan_changes_fingerprint():
    shot = Shot(
        description="x",
        acceptance={"vbench_min": 0.99},
        layers=[Layer(kind="gen", engine="comfyui", template_id="t1")],
    )
    st = ProductionState(idea="replan plantilla")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    execute_next_shot(st)

    fp_before = LineageGraph.from_dict(st.lineage).get(shot.id).fingerprint

    def reselect(s: Shot) -> bool:
        s.layers[0].template_id = "t2"
        return True

    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(
        st, StubShotEvaluator(score=0.1), reselect_template=reselect
    )  # escalón LOCAL
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(
        st, StubShotEvaluator(score=0.1), reselect_template=reselect
    )  # escalón TEMPLATE

    assert shot.replan_tier == ReplanTier.TEMPLATE
    node = LineageGraph.from_dict(st.lineage).get(shot.id)
    assert node.artifact is None
    # la plantilla es entrada del nodo: cambiarla invalida por fingerprint.
    assert node.fingerprint != fp_before


def test_global_replan_invalidates_descendants_not_siblings():
    a = Shot(description="a", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    c = Shot(description="c", layers=[Layer(kind="gen", engine="comfyui")])
    st = ProductionState(idea="invalidación selectiva")
    st.sequences.append(Sequence(title="s", shots=[a, b, c]))

    for _ in range(3):
        execute_next_shot(st)
        _accept(st)
    assert a.status == ShotStatus.ACCEPTED
    assert b.status == ShotStatus.ACCEPTED
    assert c.status == ShotStatus.ACCEPTED

    # aceptación exigente SOLO para la fase de replan: el evaluador stub (0.1)
    # debe fallar para escalar local→template→global.
    a.acceptance = {"vbench_min": 0.99}

    graph = LineageGraph.from_dict(st.lineage)
    fp_a, fp_c = graph.get(a.id).fingerprint, graph.get(c.id).fingerprint
    artifact_c = graph.get(c.id).artifact

    def replan(shot: Shot, state: ProductionState) -> bool:
        shot.description = "reencuadre global de a"
        return True

    ev = StubShotEvaluator(score=0.1)
    for _ in range(3):  # local → template → global
        a.status = ShotStatus.EVALUATING
        evaluate_and_replan(st, ev, replan_shot=replan)
    assert a.replan_tier == ReplanTier.GLOBAL

    graph = LineageGraph.from_dict(st.lineage)
    # a cambió (hipótesis nueva) y b (descendiente) queda stale...
    assert graph.get(a.id).artifact is None
    assert graph.get(a.id).fingerprint != fp_a
    assert graph.get(b.id).artifact is None
    # ...pero c, de otra rama, conserva artifact Y fingerprint intactos.
    assert graph.get(c.id).artifact == artifact_c == c.output_path
    assert graph.get(c.id).fingerprint == fp_c


def test_executor_failure_escalation_keeps_lineage_consistent():
    shot = Shot(
        description="x",
        acceptance={"vbench_min": 0.99},
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    st = ProductionState(idea="fallo en replan")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    runner = FlakyRunner()

    execute_next_shot(st, runner)  # intento 1 OK: lineage con artifact
    assert LineageGraph.from_dict(st.lineage).get(shot.id).artifact is not None

    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, StubShotEvaluator(score=0.1))  # LOCAL
    execute_next_shot(st, runner)  # intento 2 falla → escalar a TEMPLATE

    assert shot.status == ShotStatus.REPLANNING
    assert "execute_failed" in [e["kind"] for e in st.log]
    # invariante: tras cualquier escalado, lineage refleja que el artifact
    # propio fue descartado (nada de procedencia fantasma en el checkpoint).
    assert LineageGraph.from_dict(st.lineage).get(shot.id).artifact is None


def test_record_shot_lineage_without_artifact_keeps_fingerprint_only():
    shot = Shot(description="x", layers=[Layer(kind="gen", engine="comfyui")])
    st = ProductionState(idea="sin artifact")
    record_shot_lineage(st, shot)
    node = LineageGraph.from_dict(st.lineage).get(shot.id)
    assert node is not None
    assert node.fingerprint is not None
    assert node.artifact is None


class UniquePathRunner:
    """Runner determinista con ruta de salida ÚNICA por invocación."""

    def __init__(self) -> None:
        self.n = 0
        self.shots: list[str] = []

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.n += 1
        self.shots.append(str(job.get("shot")))
        return {
            "ok": True,
            "engine": engine,
            "output_path": f"/tmp/stub/{job.get('shot')}_{engine}_{self.n}.out",
        }


class RecordingAssembler:
    """Ensamblador que memoriza los clips recibidos (para probar superficie)."""

    def __init__(self) -> None:
        self.clips: list[dict] = []

    def assemble(self, production_id, clips, *, cancel_event=None):
        self.clips = [dict(c) for c in clips]
        return {"ok": True, "output_path": f"/tmp/stub/{production_id}_reel.mp4"}


def test_replan_stale_descendants_recompute_and_reassemble(tmp_path):
    # El cambio de input de A (replan global) no puede quedarse en metadata del
    # DAG: B (descendiente, ya ACEPTADO con output vivo) debe volver a
    # ejecutarse con identidad de op nueva; C (hermano) NO se toca; y el reel
    # se arma con el output NUEVO de B, jamás el viejo.
    a = Shot(description="a", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    c = Shot(description="c", layers=[Layer(kind="gen", engine="comfyui")])
    st = ProductionState(idea="invalidación con estado real")
    st.sequences.append(Sequence(title="s", shots=[a, b, c]))
    runner = UniquePathRunner()

    for _ in range(3):
        execute_next_shot(st, runner)
        _accept(st)
    assert all(s.status == ShotStatus.ACCEPTED for s in (a, b, c))
    old_b, old_c = b.output_path, c.output_path

    # replan de A con cambio de input real (GLOBAL reescribe la descripción).
    a.acceptance = {"vbench_min": 0.99}

    def replan(shot: Shot, state: ProductionState) -> bool:
        shot.description = "reencuadre global de a"
        return True

    ev_fail = StubShotEvaluator(score=0.1)
    for _ in range(3):  # local → template → global
        a.status = ShotStatus.EVALUATING
        evaluate_and_replan(st, ev_fail, replan_shot=replan)
    assert a.replan_tier == ReplanTier.GLOBAL

    # estado de producción REAL reconciliado, no solo metadata del DAG:
    assert a.output_path is None
    assert b.output_path is None
    assert all(ly.output_path is None for ly in b.layers)
    assert b.status == ShotStatus.REPLANNING
    assert [e["shot"] for e in st.log if e["kind"] == "recompute_queued"] == [b.id]
    # el hermano C queda intacto (ACCEPTED, mismo artifact):
    assert c.status == ShotStatus.ACCEPTED
    assert c.output_path == old_c

    # el checkpoint arrastra el estado reconciliado (reanudable tras corte):
    checkpointer = ProductionCheckpointer(tmp_path / "p1recon.sqlite3")
    checkpointer.save(st)
    loaded = checkpointer.load(st.id)
    assert loaded is not None
    lb = loaded.shot_by_id(b.id)
    assert lb.status == ShotStatus.REPLANNING and lb.output_path is None
    assert loaded.shot_by_id(c.id).output_path == old_c

    # recompute: A y B re-ejecutan (runner los ve de nuevo); C nunca vuelve.
    a.acceptance = {}  # la re-aceptación no debe quemar la escalera de A
    ev_ok = StubShotEvaluator(score=0.9)
    for _ in range(3):
        execute_next_shot(st, runner)
        evaluate_and_replan(st, ev_ok)
    assert a.status == ShotStatus.ACCEPTED
    assert b.status == ShotStatus.ACCEPTED
    assert runner.shots.count(b.id) == 2  # ejecución inicial + recompute
    assert runner.shots.count(c.id) == 1  # solo la inicial
    assert b.output_path != old_b

    # ensamblaje: clips con el output NUEVO de B y el de C intacto.
    assembler = RecordingAssembler()
    assemble_reel(st, assembler)
    assert st.phase == "done"
    clip_paths = {c["shot"]: c["path"] for c in assembler.clips}
    assert clip_paths[b.id] == b.output_path
    assert clip_paths[c.id] == old_c
    assert old_b not in clip_paths.values()


def test_replan_does_not_resurrect_failed_descendant():
    # un descendiente FAILED es terminal: la invalidación no lo resucita.
    a = Shot(description="a", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    b.status = ShotStatus.FAILED
    st = ProductionState(idea="no resucitar")
    st.sequences.append(Sequence(title="s", shots=[a, b]))
    execute_next_shot(st)  # A computa y queda en lineage
    _accept(st)

    a.acceptance = {"vbench_min": 0.99}
    a.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, StubShotEvaluator(score=0.1))  # LOCAL: sin cambios
    a.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, StubShotEvaluator(score=0.1))  # TEMPLATE: sin cambios
    a.status = ShotStatus.EVALUATING
    evaluate_and_replan(
        st,
        StubShotEvaluator(score=0.1),
        replan_shot=lambda s, st_: (setattr(s, "description", "nuevo"), True)[1],
    )  # GLOBAL: input cambia

    assert b.status == ShotStatus.FAILED
    assert "recompute_queued" not in [e["kind"] for e in st.log]


# --- 2. gasto acumulativo entre escalones -----------------------------------


def test_spend_is_cumulative_across_escalation_tiers():
    shot = Shot(
        description="x",
        acceptance={"vbench_min": 0.99},
        cost_cap_usd=2.0,
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    st = ProductionState(idea="gasto acumulativo")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    runner = CostRunner(cost_usd=0.4)
    ev = StubShotEvaluator(score=0.1)

    execute_next_shot(st, runner)  # intento inicial (sin escalón)
    for _ in range(3):  # local → template → global
        shot.status = ShotStatus.EVALUATING
        evaluate_and_replan(st, ev)
        execute_next_shot(st, runner)

    # 4 intentos × 0.4: el gasto NUNCA se reinicia entre escalones.
    assert shot.cost_spent_usd == 1.6
    assert shot.spend_reported is True
    # cada intento recibe el cap RESTANTE, no el original.
    assert runner.caps == [2.0, 1.6, 1.2, 0.8]
    spend_events = [e for e in st.log if e["kind"] == "layer_spend"]
    assert [e["cumulative_usd"] for e in spend_events] == [0.4, 0.8, 1.2, 1.6]
    assert [e["tier"] for e in spend_events] == [
        None,
        str(ReplanTier.LOCAL),
        str(ReplanTier.TEMPLATE),
        str(ReplanTier.GLOBAL),
    ]


def test_budget_exhausted_fails_closed_without_invoking_runner():
    # P1: alcanzado/superado el cap positivo, el runner NO se invoca ni una
    # vez más (0 como cost_cap es ambiguo aguas abajo: puede leerse "sin tope").
    shot = Shot(
        description="x",
        acceptance={"vbench_min": 0.99},
        cost_cap_usd=0.5,
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    st = ProductionState(idea="cap agotado")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    runner = CostRunner(cost_usd=0.4)
    ev = StubShotEvaluator(score=0.1)

    execute_next_shot(st, runner)  # intento inicial: cap 0.5 → gasta 0.4
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev)  # LOCAL
    execute_next_shot(st, runner)  # cap restante 0.1 → gasta 0.4 (total 0.8)
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev)  # TEMPLATE
    execute_next_shot(st, runner)  # 0.8 >= 0.5: SIN llamada → escalar a GLOBAL
    execute_next_shot(st, runner)  # agotado de nuevo → sin llamada → FAILED

    assert runner.caps == [0.5, 0.1]  # jamás recibió 0.0
    assert shot.status == ShotStatus.FAILED
    assert shot.cost_spent_usd == 0.8  # sin gasto fantasma tras el corte
    events = [e for e in st.log if e["kind"] == "budget_exhausted"]
    assert len(events) == 2
    assert events[0]["tier"] == str(ReplanTier.GLOBAL)
    assert events[1]["tier"] is None
    assert events[0]["cost_spent_usd"] == 0.8


def test_escalate_replan_preserves_spend_and_roundtrips():
    shot = Shot(
        description="x",
        cost_cap_usd=1.0,
        cost_spent_usd=0.6,
        spend_reported=True,
    )
    shot.escalate_replan()
    assert shot.cost_spent_usd == 0.6  # el gasto sobrevive al escalón
    assert shot.spend_reported is True

    st = ProductionState(idea="gasto persistente")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    loaded = ProductionState.from_dict(st.to_dict())
    assert loaded.all_shots()[0].cost_spent_usd == 0.6
    assert loaded.all_shots()[0].spend_reported is True
    # back-compat: un checkpoint previo a los campos carga con 0.0/False.
    old = st.to_dict()
    del old["sequences"][0]["shots"][0]["cost_spent_usd"]
    del old["sequences"][0]["shots"][0]["spend_reported"]
    legacy = ProductionState.from_dict(old)
    assert legacy.all_shots()[0].cost_spent_usd == 0.0
    assert legacy.all_shots()[0].spend_reported is False


# --- 3. propósito ambiguo: hipótesis preservada o razón tipada ---------------


def test_empty_plan_with_hypothesis_falls_back_to_conservative_complete():
    st = ProductionState(idea="gato astronauta sobre una nebulosa")
    plan_production(st, EmptyPlanner())

    # plan conservador COMPLETO: estructura determinista apertura/medio/cierre.
    assert len(st.sequences) == 1
    assert len(st.all_shots()) == 3
    assert st.all_shots()[0].layers, "cada shot del plan conservador tiene capas"
    # la hipótesis se preserva: verbatim en state.idea, en el log del fallback
    # y en las descripciones de los shots.
    event = next(e for e in st.log if e["kind"] == "plan_conservative_fallback")
    assert event["reason"] == str(PlanTerminalReason.AMBIGUOUS_PURPOSE)
    assert event["hypothesis"] == st.idea
    assert all(st.idea in s.description for s in st.all_shots())
    assert st.phase == "executing"


def test_blank_idea_terminates_with_typed_reason_not_fake_plan():
    st = ProductionState(idea="   ")
    plan_production(st, EmptyPlanner())

    assert st.phase == "failed"
    assert st.sequences == []
    event = st.log[-1]
    assert event["kind"] == "plan_rejected"
    assert event["reason"] == "ambiguous_purpose"
    # la razón es tipada: round-trip por el enum.
    assert PlanTerminalReason(event["reason"]) is PlanTerminalReason.AMBIGUOUS_PURPOSE


def test_service_plan_without_hypothesis_returns_typed_terminal(tmp_path):
    # un planner real que no determina el propósito (plan vacío) ante una idea
    # sin hipótesis preservable: terminal tipado en la frontera del servicio.
    service = ProductionService(
        checkpointer=ProductionCheckpointer(tmp_path / "p1.db"),
        collaborators=Collaborators(planner=EmptyPlanner()),
    )
    result = service.plan_production("   ")

    assert result["phase"] == "failed"
    assert result["terminal_reason"] == "ambiguous_purpose"
    # no hay puerta ni aprobación para un plan imposible; y quedará terminal.
    loaded = service.checkpointer.load(result["production_id"])
    assert loaded is not None and loaded.phase == "failed"
    with pytest.raises(ProductionAlreadyTerminalError):
        service.approve_plan(result["production_id"])


def test_status_distinguishes_reported_spend_from_unknown():
    # con un runner que REPORTA coste: el gasto acumulado es visible.
    st = ProductionState(idea="gasto reportado")
    shot = Shot(
        description="x", cost_cap_usd=1.0, layers=[Layer(kind="gen", engine="comfyui")]
    )
    st.sequences.append(Sequence(title="s", shots=[shot]))
    execute_next_shot(st, CostRunner(cost_usd=0.3))

    status = ProductionService._status_dict(st)
    assert status["spend_reported"] is True
    assert status["cost_spent_usd"] == 0.3

    # Cero también puede ser un costo medido; no confundirlo con ausencia de
    # medición del runner.
    st_zero = ProductionState(idea="costo cero reportado")
    st_zero.sequences.append(
        Sequence(
            title="s",
            shots=[Shot(description="z", layers=[Layer(kind="gen", engine="comfyui")])],
        )
    )
    execute_next_shot(st_zero, CostRunner(cost_usd=0.0))
    status_zero = ProductionService._status_dict(st_zero)
    assert status_zero["spend_reported"] is True
    assert status_zero["cost_spent_usd"] == 0.0

    # con el stub por defecto (que NO reporta cost_usd — igual que los
    # adaptadores de motor reales): el gasto es DESCONOCIDO, no 0.0. Exponer
    # 0.0 afirmaría un gasto que nunca se midió (P1).
    st2 = ProductionState(idea="gasto no reportado")
    shot2 = Shot(
        description="y", cost_cap_usd=1.0, layers=[Layer(kind="gen", engine="comfyui")]
    )
    st2.sequences.append(Sequence(title="s", shots=[shot2]))
    execute_next_shot(st2)  # StubEngineRunner no reporta cost_usd

    status2 = ProductionService._status_dict(st2)
    assert status2["spend_reported"] is False
    assert status2["cost_spent_usd"] is None
