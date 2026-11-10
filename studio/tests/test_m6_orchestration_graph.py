"""Tests del Bloque 4 fase 12 — base de orquestación LangGraph.

Cubre el estado tipado (round-trip + escalones de replan), el checkpointer SQLite
(reanudación tras corte) y el grafo esqueleto corriendo de punta a punta.
"""

from __future__ import annotations

from comfy_studio.orchestration import (
    REPLAN_ESCALATION,
    Layer,
    ProductionCheckpointer,
    ProductionState,
    ReplanTier,
    Sequence,
    Shot,
    ShotStatus,
    build_production_graph,
    langgraph_available,
)

# --- estado tipado ---------------------------------------------------------


def _sample_state() -> ProductionState:
    shot = Shot(
        description="dron sobre la ciudad al amanecer",
        duration_s=6.0,
        acceptance={"vbench_min": 0.6},
        depends_on=[],
        layers=[
            Layer(kind="gen", engine="comfyui", template_id="t1", params={"seed": 7}),
            Layer(kind="upscale", engine="upscaler", params={"mode": "video"}),
        ],
        cost_cap_usd=0.5,
    )
    seq = Sequence(title="apertura", shots=[shot])
    st = ProductionState(idea="reel de ciudad futurista", cost_cap_usd=5.0)
    st.sequences.append(seq)
    return st


def test_state_roundtrip_preserves_everything():
    st = _sample_state()
    st.record("note", detail="arranque")
    restored = ProductionState.from_dict(st.to_dict())

    assert restored.idea == st.idea
    assert restored.id == st.id
    assert restored.cost_cap_usd == 5.0
    assert len(restored.sequences) == 1
    shot = restored.all_shots()[0]
    assert shot.description == "dron sobre la ciudad al amanecer"
    assert shot.duration_s == 6.0
    assert shot.acceptance == {"vbench_min": 0.6}
    assert len(shot.layers) == 2
    assert shot.layers[0].template_id == "t1"
    assert shot.layers[0].params == {"seed": 7}
    assert restored.log and restored.log[-1]["kind"] == "note"


def test_shot_lookup_and_pending():
    st = _sample_state()
    shot = st.all_shots()[0]
    assert st.shot_by_id(shot.id) is shot
    assert st.shot_by_id("nope") is None
    assert st.pending_shots() == [shot]
    assert not st.is_complete()
    shot.status = ShotStatus.ACCEPTED
    assert st.is_complete()


def test_replan_escalation_order_and_exhaustion():
    shot = Shot(description="x")
    # local -> template -> global -> None (fallado)
    assert shot.escalate_replan() == ReplanTier.LOCAL
    assert shot.status == ShotStatus.REPLANNING
    assert shot.escalate_replan() == ReplanTier.TEMPLATE
    assert shot.escalate_replan() == ReplanTier.GLOBAL
    assert shot.replan_attempts == 3
    assert shot.escalate_replan() is None
    assert shot.status == ShotStatus.FAILED


def test_replan_escalation_matches_user_order():
    assert REPLAN_ESCALATION == (
        ReplanTier.LOCAL,
        ReplanTier.TEMPLATE,
        ReplanTier.GLOBAL,
    )


# --- checkpointer ----------------------------------------------------------


def test_checkpointer_save_load_resume(tmp_path):
    db = tmp_path / "ckpt.sqlite3"
    cp = ProductionCheckpointer(db_path=db)
    st = _sample_state()
    cp.save(st)

    loaded = cp.load(st.id)
    assert loaded is not None
    assert loaded.id == st.id
    assert loaded.all_shots()[0].description == st.all_shots()[0].description

    # upsert: cambiar fase y re-guardar no duplica.
    st.phase = "executing"
    cp.save(st)
    again = cp.load(st.id)
    assert again.phase == "executing"
    assert cp.list_ids() == [st.id]


def test_checkpointer_missing_and_delete(tmp_path):
    cp = ProductionCheckpointer(db_path=tmp_path / "c.sqlite3")
    assert cp.load("ghost") is None
    st = _sample_state()
    cp.save(st)
    assert cp.delete(st.id) is True
    assert cp.delete(st.id) is False
    assert cp.load(st.id) is None


def test_checkpointer_prune_keep_max_and_age(tmp_path):
    import time

    db = tmp_path / "prune.sqlite3"
    cp = ProductionCheckpointer(db_path=db)

    # Crear 5 estados distintos
    for i in range(5):
        st = _sample_state()
        st.id = f"prod_{i}"
        cp.save(st)
        time.sleep(0.01)

    assert len(cp.list_ids()) == 5

    # Poda manteniendo los 3 más recientes
    deleted = cp.prune(keep_max=3)
    assert deleted == 2
    assert len(cp.list_ids()) == 3

    # Poda por días (ninguno tiene más de 1 día de antigüedad)
    del_days = cp.prune(keep_max=10, older_than_days=1.0)
    assert del_days == 0
    assert len(cp.list_ids()) == 3


# --- grafo esqueleto -------------------------------------------------------


def test_graph_runs_end_to_end(tmp_path):
    cp = ProductionCheckpointer(db_path=tmp_path / "g.sqlite3")
    graph = build_production_graph(checkpointer=cp)
    st = ProductionState(idea="gato astronauta en neon")

    final = graph.run(st)

    assert final.phase == "done"
    assert final.final_output_path is not None
    assert final.is_complete()
    assert final.all_shots()[0].status == ShotStatus.ACCEPTED
    # el lazo dejo su rastro de decisiones.
    kinds = [e["kind"] for e in final.log]
    assert "plan" in kinds
    assert "execute" in kinds
    assert "evaluate" in kinds
    assert "assemble" in kinds


def test_langgraph_result_updates_existing_state_references():
    from comfy_studio.orchestration.graph import _apply_state_in_place

    original = _sample_state()
    sequence = original.sequences[0]
    shot = sequence.shots[0]
    layer = shot.layers[0]
    result = ProductionState.from_dict(original.to_dict())
    result_shot = result.sequences[0].shots[0]
    result_shot.status = ShotStatus.FAILED
    result_shot.layers[0].output_path = "/tmp/result.png"

    returned = _apply_state_in_place(original, result)

    assert returned is original
    assert original.sequences[0] is sequence
    assert original.sequences[0].shots[0] is shot
    assert shot.status is ShotStatus.FAILED
    assert shot.layers[0] is layer
    assert layer.output_path == "/tmp/result.png"


def test_installed_langgraph_is_the_runtime_backend(tmp_path, monkeypatch):
    import pytest

    if not langgraph_available():
        pytest.skip("LangGraph is installed in the production Studio image")

    from comfy_studio.orchestration.graph import ProductionGraph

    invoked: list[bool] = []
    original_compile = ProductionGraph.compile_langgraph

    def compile_graph(self):
        invoked.append(True)
        return original_compile(self)

    monkeypatch.setattr(ProductionGraph, "compile_langgraph", compile_graph)
    checkpointer = ProductionCheckpointer(tmp_path / "langgraph.sqlite3")
    final = build_production_graph(checkpointer=checkpointer).run(
        ProductionState(idea="backend real LangGraph")
    )

    assert invoked
    assert final.phase == "done"
    assert checkpointer.load(final.id).phase == "done"


def test_graph_checkpoints_each_step(tmp_path):
    cp = ProductionCheckpointer(db_path=tmp_path / "g2.sqlite3")
    graph = build_production_graph(checkpointer=cp)
    st = ProductionState(idea="desierto rojo")
    final = graph.run(st)
    # tras correr, el checkpoint refleja el estado final reanudable.
    resumed = cp.load(final.id)
    assert resumed is not None
    assert resumed.phase == "done"
    assert resumed.final_output_path == final.final_output_path


def test_graph_halts_on_max_steps():
    graph = build_production_graph(max_steps=1)
    st = ProductionState(idea="loop guard")
    final = graph.run(st)
    # un solo paso no alcanza a terminar -> no se cuelga.
    assert final.phase in ("halted", "executing", "planning")


def test_langgraph_available_is_boolean():
    assert isinstance(langgraph_available(), bool)



# --------------------------------------------------------------------------
# El planner es un LLM: sus params no pueden falsificar la identidad del job
# --------------------------------------------------------------------------


class _CapturingRunner:
    """EngineRunner que solo registra el job que recibió."""

    def __init__(self) -> None:
        self.jobs: list[dict] = []

    def run_layer(
        self,
        engine: str,
        job: dict,
        *,
        cost_cap: float | None = None,
        cancel_event=None,
    ) -> dict:
        self.jobs.append(dict(job))
        return {"ok": True, "engine": engine, "output_path": "/tmp/out.bin"}


def _state_with_params(params: dict, *, project_id: str = "proj-real") -> ProductionState:
    """Producción con un shot cuya capa lleva `params` (el planner es un LLM)."""
    state = ProductionState(idea="prueba")
    shot = Shot(id="shot-real", description="d", duration_s=1.0)
    shot.status = ShotStatus.PLANNED
    shot.layers = [
        Layer(
            kind="gen",
            engine="comfyui",
            template_id="tpl-real",
            params=dict(params),
        )
    ]
    state.sequences = [Sequence(title="s", shots=[shot])]
    state.metadata["project_id"] = project_id
    return state


def test_planner_params_cannot_forge_orchestration_owned_job_keys():
    """Un params del planner con kind/shot/job_id/... falsificados no debe
    sobreescribir la identidad que la orquestación pone en el job."""
    from comfy_studio.orchestration.executor import execute_next_shot

    runner = _CapturingRunner()
    state = _state_with_params(
        {
            "prompt": "un gato",          # legítimo: debe pasar
            "seed": 7,                    # legítimo: debe pasar
            "kind": "forged-kind",        # reservado: debe ignorarse
            "shot": "forged-shot",        # reservado: debe ignorarse
            "job_id": "forged-job",       # reservado: debe ignorarse
            "idempotency_key": "forged-idem",
            "project_id": "forged-proj",  # reservado: debe ignorarse
            "template_id": "forged-tpl",  # reservado: debe ignorarse
        }
    )

    execute_next_shot(state, runner=runner)

    assert runner.jobs, "no se ejecutó ninguna capa"
    job = runner.jobs[0]
    # Identidad real de la orquestación.
    assert job["kind"] == "gen"
    assert job["shot"] == "shot-real"
    assert job["project_id"] == "proj-real"
    assert job["template_id"] == "tpl-real"
    # Ningún valor falsificado sobrevivió.
    for key in ("kind", "shot", "job_id", "idempotency_key", "project_id", "template_id"):
        assert "forged" not in str(job.get(key)), f"{key} falsificada: {job.get(key)!r}"
    # Los params legítimos sí pasaron.
    assert job["prompt"] == "un gato"
    assert job["seed"] == 7


def test_planner_params_pass_through_unchanged_when_not_reserved():
    """El filtro solo descarta las claves reservadas; lo demás pasa intacto."""
    from comfy_studio.orchestration.executor import _RESERVED_JOB_KEYS, execute_next_shot

    legit = {
        "prompt": "un perro",
        "seed": 42,
        "steps": 30,
        "output_path": "out/video.mp4",
        "negative_prompt": "borroso",
        "cfg": 7.5,
    }
    runner = _CapturingRunner()
    state = _state_with_params(legit)

    execute_next_shot(state, runner=runner)

    job = runner.jobs[0]
    for key, value in legit.items():
        assert job[key] == value, f"{key} alterado: {job[key]!r} != {value!r}"
    # Las reservadas siguen siendo las de la orquestación.
    assert job["kind"] == "gen"
    assert job["shot"] == "shot-real"
    assert "forged" not in str(job["job_id"])
    assert set(_RESERVED_JOB_KEYS) == {
        "kind",
        "shot",
        "job_id",
        "idempotency_key",
        "project_id",
        "template_id",
    }
