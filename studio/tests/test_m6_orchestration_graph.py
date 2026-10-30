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
