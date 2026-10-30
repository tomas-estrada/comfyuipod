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
    TemplateSelector,
    assemble_reel,
    build_production_graph,
    evaluate_and_replan,
    execute_next_shot,
    make_template_reselector,
    plan_production,
    replan_shot_focalized,
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


# --- auditoría M6: robustez del lazo ---------------------------------------


def test_missing_dependency_is_unreachable_not_deadlock():
    # M6-SCR-001: una dep INEXISTENTE (id alucinado) no está en failed ni
    # accepted -> antes se saltaba para siempre; ahora falla en claro.
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = ["shot_inexistente_99"]
    st = ProductionState(idea="hallu")
    st.sequences.append(Sequence(title="s", shots=[b]))
    execute_next_shot(st)
    assert b.status == ShotStatus.FAILED
    assert "shot_inexistente_99" in (b.error or "")
    assert "shot_unreachable" in [e["kind"] for e in st.log]


def test_graph_with_missing_dep_terminates_not_max_steps():
    b = Shot(description="b", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = ["ghost"]
    st = ProductionState(idea="hallu2")
    st.sequences.append(Sequence(title="s", shots=[b]))
    graph = build_production_graph(max_steps=50)
    final = graph.run(st)
    assert final.phase in ("done", "failed")  # terminó, no halted
    assert b.status == ShotStatus.FAILED


def test_assembler_failure_gives_up_to_failed_not_infinite_loop():
    # M6-SCR-004: un ensamblador que siempre falla no debe girar hasta max_steps;
    # tras agotar reintentos marca phase=failed y el router termina.
    class FailAsm:
        def assemble(self, production_id, clips):
            return {"ok": False, "error": "ffmpeg murió"}

    s1 = Shot(description="a", output_path="/tmp/a.mp4")
    s1.status = ShotStatus.ACCEPTED
    st = ProductionState(idea="asm-fail")
    st.sequences.append(Sequence(title="s", shots=[s1]))
    col = Collaborators(assembler=FailAsm())
    graph = build_production_graph(collaborators=col, max_steps=50)
    final = graph.run(st)
    assert final.phase == "failed"  # terminó, no halted
    assert int(final.metadata.get("assemble_attempts", 0)) >= 2
    assert "assemble_giveup" in [e["kind"] for e in final.log]


def test_full_graph_default_stubs_reaches_done():
    graph = build_production_graph()
    st = ProductionState(idea="desierto rojo al atardecer", cost_cap_usd=2.0)
    final = graph.run(st)
    assert final.phase == "done"
    assert final.final_output_path is not None
    assert all(s.status == ShotStatus.ACCEPTED for s in final.all_shots())


# --- M6 Producción: adaptadores y replan focalizado ------------------------


def test_make_template_reselector_swaps_viable_template():
    sel = TemplateSelector()
    reselector = make_template_reselector(sel)
    shot = Shot(
        description="un coche veloz en la noche",
        layers=[Layer(kind="gen", engine="comfyui", template_id="wan_t2v")],
    )
    swapped = reselector(shot)
    assert swapped is True
    assert shot.layers[0].template_id != "wan_t2v"


def test_replan_shot_focalized_preserves_sequences_and_deps():
    a = Shot(description="primer plano", layers=[Layer(kind="gen", engine="comfyui")])
    b = Shot(description="segundo plano", layers=[Layer(kind="gen", engine="comfyui")])
    b.depends_on = [a.id]
    st = ProductionState(idea="película nocturna")
    st.sequences.append(Sequence(title="secuencia 1", shots=[a, b]))

    ok = replan_shot_focalized(b, st)
    assert ok is True
    assert b.depends_on == [a.id]
    assert len(st.sequences) == 1
    assert len(st.all_shots()) == 2
    assert b.description != ""


def test_evaluate_and_replan_global_triggers_focal_replanner():
    class AlwaysFailEvaluator(StubShotEvaluator):
        def evaluate(self, shot):
            return {"vbench": 0.1}

    shot = Shot(
        description="plano complejo",
        acceptance={"vbench_min": 0.8},
        layers=[Layer(kind="gen", engine="comfyui")],
    )
    st = ProductionState(idea="test")
    st.sequences.append(Sequence(title="s", shots=[shot]))

    focal_calls = []

    def dummy_replan(s, state):
        focal_calls.append(s.id)
        return True

    # 1. LOCAL
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, AlwaysFailEvaluator())
    assert shot.status == ShotStatus.REPLANNING

    # 2. TEMPLATE
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, AlwaysFailEvaluator())
    assert shot.status == ShotStatus.REPLANNING

    # 3. GLOBAL (debe invocar replan_shot focalizado)
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(st, AlwaysFailEvaluator(), replan_shot=dummy_replan)
    assert shot.status == ShotStatus.REPLANNING
    assert focal_calls == [shot.id]
    assert st.log[-1]["focal_replanned"] is True
