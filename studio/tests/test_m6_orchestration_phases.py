"""Tests del Bloque 4 fases 13–16 — planner, ejecución, feedback loop, ensamblaje.

Cubre la planificación jerárquica, el orden topológico de ejecución, el lazo de
replanificación escalonado local→plantilla→global de punta a punta, y el
ensamblaje del reel.
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration import (
    Collaborators,
    CyclicDependencyError,
    Layer,
    ProductionState,
    ReplanTier,
    Sequence,
    Shot,
    ShotStatus,
    StubShotEvaluator,
    assemble_reel,
    build_production_graph,
    evaluate_and_replan,
    execute_next_shot,
    plan_production,
    topological_shots,
)

# --- fase 13: planner jerárquico -------------------------------------------


def test_planner_decomposes_idea_into_hierarchy():
    st = ProductionState(idea="un dron sobrevuela la ciudad de noche", cost_cap_usd=3.0)
    plan_production(st)
    assert st.sequences
    shots = st.all_shots()
    assert len(shots) >= 1
    # señales 'dron' (depth) + 'noche' (relight) -> capas extra.
    kinds = {ly.kind for ly in shots[0].layers}
    assert "gen" in kinds
    assert "depth" in kinds  # 'dron'
    assert "relight" in kinds  # 'noche'
    # cost cap repartido por shot.
    assert all(s.cost_cap_usd > 0 for s in shots)
    assert abs(sum(s.cost_cap_usd for s in shots) - 3.0) < 0.01
    # cada shot tiene umbral de aceptación.
    assert all("vbench_min" in s.acceptance for s in shots)


def test_planner_is_idempotent():
    st = ProductionState(idea="x")
    plan_production(st)
    n = len(st.all_shots())
    plan_production(st)  # segunda vez no duplica
    assert len(st.all_shots()) == n


# --- fase 14: ejecución adaptativa + orden topológico ----------------------


def test_topological_order_honors_depends_on():
    a = Shot(description="a")
    b = Shot(description="b")
    c = Shot(description="c")
    b.depends_on = [a.id]
    c.depends_on = [b.id]
    st = ProductionState(idea="dep")
    st.sequences.append(Sequence(title="s", shots=[c, b, a]))  # orden inverso
    ordered = [s.id for s in topological_shots(st)]
    assert ordered.index(a.id) < ordered.index(b.id) < ordered.index(c.id)


def test_cyclic_dependency_is_rejected():
    a = Shot(description="a")
    b = Shot(description="b")
    a.depends_on = [b.id]
    b.depends_on = [a.id]
    st = ProductionState(idea="cycle")
    st.sequences.append(Sequence(title="s", shots=[a, b]))
    with pytest.raises(CyclicDependencyError):
        topological_shots(st)


def test_execute_waits_for_dependencies():
    a = Shot(description="a", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    st = ProductionState(idea="dep")
    st.sequences.append(Sequence(title="s", shots=[a, b]))
    # primera ejecución: solo 'a' puede correr (b espera a a).
    execute_next_shot(st)
    assert a.status == ShotStatus.EVALUATING
    assert b.status == ShotStatus.PLANNED


def test_execute_failing_runner_triggers_replanning():
    class FailRunner:
        def run_layer(self, engine, job, *, cost_cap=None):
            return {"ok": False, "error": "boom"}

    shot = Shot(description="x", layers=[Layer(kind="gen", engine="comfyui")])
    st = ProductionState(idea="f")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    execute_next_shot(st, FailRunner())
    assert shot.status == ShotStatus.REPLANNING
    assert shot.error == "boom"


def test_failed_dependency_propagates_as_unreachable():
    # A falló definitivamente; B depende de A -> B es inalcanzable (FAILED),
    # no se queda pendiente para siempre.
    a = Shot(description="a")
    a.status = ShotStatus.FAILED
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    st = ProductionState(idea="dep-fail")
    st.sequences.append(Sequence(title="s", shots=[a, b]))
    execute_next_shot(st)
    assert b.status == ShotStatus.FAILED
    assert a.id in (b.error or "")
    kinds = [e["kind"] for e in st.log]
    assert "shot_unreachable" in kinds


def test_graph_with_unsatisfiable_dep_reaches_done_not_max_steps():
    # un runner que siempre falla + un evaluador exigente agota el replan del
    # primer shot; el segundo (dependiente) queda inalcanzable; el grafo TERMINA
    # (ensambla lo que haya, phase=done) en vez de girar hasta max_steps.
    class AlwaysFail:
        def run_layer(self, engine, job, *, cost_cap=None):
            return {"ok": False, "error": "nope"}

    a = Shot(
        description="a",
        acceptance={"vbench_min": 0.99},
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    st = ProductionState(idea="unsat")
    st.sequences.append(Sequence(title="s", shots=[a, b]))
    col = Collaborators(runner=AlwaysFail())
    graph = build_production_graph(collaborators=col, max_steps=50)
    final = graph.run(st)
    assert final.phase == "done"  # terminó, no halted
    assert a.status == ShotStatus.FAILED
    assert b.status == ShotStatus.FAILED


# --- fase 15: feedback loop + replan escalonado ----------------------------


def test_accept_when_score_passes():
    shot = Shot(description="x", acceptance={"vbench_min": 0.5})
    shot.status = ShotStatus.EVALUATING
    st = ProductionState(idea="a")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    evaluate_and_replan(st, StubShotEvaluator(score=0.9))
    assert shot.status == ShotStatus.ACCEPTED
    assert shot.last_score == {"vbench": 0.9}


def test_staged_replan_escalates_local_template_global_then_fails():
    # score siempre por debajo del umbral -> agota los 3 escalones.
    shot = Shot(description="x", acceptance={"vbench_min": 0.99})
    shot.status = ShotStatus.EVALUATING
    st = ProductionState(idea="hard")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    ev = StubShotEvaluator(score=0.1)

    evaluate_and_replan(st, ev)
    assert shot.replan_tier == ReplanTier.LOCAL
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev)
    assert shot.replan_tier == ReplanTier.TEMPLATE
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev)
    assert shot.replan_tier == ReplanTier.GLOBAL
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev)
    assert shot.status == ShotStatus.FAILED
    kinds = [e["kind"] for e in st.log]
    assert kinds.count("replan") == 3
    assert "replan_exhausted" in kinds


def test_template_reselect_is_invoked_at_template_tier():
    calls: list[str] = []

    def reselect(shot):
        calls.append(shot.id)
        return True

    shot = Shot(description="x", acceptance={"vbench_min": 0.99})
    shot.status = ShotStatus.EVALUATING
    st = ProductionState(idea="t")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    ev = StubShotEvaluator(score=0.1)
    evaluate_and_replan(st, ev)  # local
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, ev, reselect_template=reselect)  # template
    assert calls == [shot.id]


# --- fase 16: ensamblaje ---------------------------------------------------


def test_assemble_accepted_shots():
    s1 = Shot(description="a", output_path="/tmp/a.mp4")
    s1.status = ShotStatus.ACCEPTED
    s2 = Shot(description="b", output_path="/tmp/b.mp4")
    s2.status = ShotStatus.ACCEPTED
    st = ProductionState(idea="r")
    st.sequences.append(Sequence(title="s", shots=[s1, s2]))
    assemble_reel(st)
    assert st.phase == "done"
    assert st.final_output_path is not None


def test_assemble_no_accepted_shots_leaves_no_reel():
    shot = Shot(description="x")
    shot.status = ShotStatus.FAILED
    st = ProductionState(idea="r")
    st.sequences.append(Sequence(title="s", shots=[shot]))
    assemble_reel(st)
    assert st.phase == "done"
    assert st.final_output_path is None


# --- integración: grafo completo con replan real --------------------------


def test_full_graph_with_replan_reaches_done():
    # evaluador que falla la primera vez y pasa tras un replan local.
    class FlakyEval:
        def __init__(self):
            self.n = 0

        def evaluate(self, shot):
            self.n += 1
            return {"vbench": 0.2 if self.n == 1 else 0.9}

    col = Collaborators(evaluator=FlakyEval())
    graph = build_production_graph(collaborators=col)
    st = ProductionState(idea="gato astronauta")
    final = graph.run(st)
    assert final.phase == "done"
    assert final.final_output_path is not None
    # hubo al menos un replan antes de aceptar.
    assert any(e["kind"] == "replan" for e in final.log)
    assert final.is_complete()


def test_full_graph_default_stubs_reaches_done():
    graph = build_production_graph()
    st = ProductionState(idea="desierto rojo al atardecer", cost_cap_usd=2.0)
    final = graph.run(st)
    assert final.phase == "done"
    assert final.final_output_path is not None
    assert all(s.status == ShotStatus.ACCEPTED for s in final.all_shots())
