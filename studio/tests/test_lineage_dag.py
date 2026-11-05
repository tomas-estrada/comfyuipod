"""DAG de producción con invalidación selectiva (plan Ultra, W04 / A05).

Verifica el objetivo concreto de la política: «cambiar un título no regenera
actores; cambiar iluminación no recalcula transcripción». Puro cálculo en CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration.lineage import LineageGraph


def _studio_graph() -> LineageGraph:
    """DAG realista: brief -> {asr, geometry}; lighting -> comp; title -> titlecard.

    Estructura:
        brief ------> asr
              \\----> geometry ----> render ----> comp ----> final
        lighting --------------------------------^
        title ----> titlecard --------------------------------> final
    """
    g = LineageGraph()
    g.add("brief", "brief", inputs={"text": "dron costero atardecer"})
    g.add("asr", "asr", inputs={"audio": "voz.wav"}, depends_on=["brief"])
    g.add("geometry", "geometry", inputs={"place": "puente_4sur"}, depends_on=["brief"])
    g.add("lighting", "lighting", inputs={"key": 5000})
    g.add("render", "render", inputs={"seed": 7}, depends_on=["geometry"])
    g.add("comp", "comp", inputs={}, depends_on=["render", "lighting"])
    g.add("title", "title", inputs={"text": "ATARDECER"})
    g.add("titlecard", "titlecard", inputs={}, depends_on=["title"])
    g.add("final", "final", inputs={}, depends_on=["comp", "titlecard"])
    return g


def test_topo_order_respects_dependencies() -> None:
    g = _studio_graph()
    order = g.topo_order()
    assert order.index("brief") < order.index("asr")
    assert order.index("geometry") < order.index("render") < order.index("comp")
    assert order.index("comp") < order.index("final")
    assert order.index("titlecard") < order.index("final")


def test_cycle_is_detected() -> None:
    g = LineageGraph()
    g.add("a", "x", depends_on=["b"])
    g.add("b", "x", depends_on=["a"])
    with pytest.raises(ValueError, match="ciclo"):
        g.topo_order()


def test_changing_the_title_does_not_recompute_asr_or_geometry() -> None:
    """A05: cambiar el título no recalcula ASR ni geometría (ni actores)."""
    g = _studio_graph()
    baseline = g.compute_fingerprints()
    # Se marca todo como computado (artifacts presentes).
    for n in g.nodes():
        g.set_computed(n.node_id, f"/out/{n.node_id}.bin")

    # El usuario cambia el TÍTULO.
    g.update_inputs("title", text="AMANECER")
    stale = g.plan_recompute(baseline)

    # Stale: solo el título y lo que cuelga de él (titlecard, final).
    assert stale == {"title", "titlecard", "final"}
    # ASR y geometría NO se tocan: su transcripción/geometría se conserva.
    assert "asr" not in stale
    assert "geometry" not in stale
    assert "render" not in stale

    g.invalidate_artifacts(stale)
    # Los no afectados conservan su artifact (no se regeneran).
    assert g.get("asr").artifact == "/out/asr.bin"
    assert g.get("geometry").artifact == "/out/geometry.bin"
    assert g.get("render").artifact == "/out/render.bin"
    # Los stale quedan sin artifact (hay que recomputarlos).
    assert g.get("title").artifact is None
    assert g.get("final").artifact is None


def test_changing_lighting_only_invalidates_its_dependents() -> None:
    """A05: cambiar iluminación solo invalida sus dependientes (comp, final)."""
    g = _studio_graph()
    baseline = g.compute_fingerprints()
    for n in g.nodes():
        g.set_computed(n.node_id, f"/out/{n.node_id}.bin")

    g.update_inputs("lighting", key=3200)
    stale = g.plan_recompute(baseline)

    assert stale == {"lighting", "comp", "final"}
    # La transcripción (ASR) NO se recalcula al cambiar la luz.
    assert "asr" not in stale
    assert "geometry" not in stale
    assert "render" not in stale
    assert "titlecard" not in stale


def test_no_change_means_nothing_is_stale() -> None:
    g = _studio_graph()
    baseline = g.compute_fingerprints()
    # Sin cambios, recomputar el plan no marca nada.
    assert g.plan_recompute(baseline) == set()


def test_change_propagates_only_downstream_not_laterally() -> None:
    """Un cambio en geometría invalida render/comp/final, no asr ni titlecard."""
    g = _studio_graph()
    baseline = g.compute_fingerprints()
    g.update_inputs("geometry", place="corredor_las_palmas")
    stale = g.plan_recompute(baseline)
    assert "geometry" in stale
    assert "render" in stale and "comp" in stale and "final" in stale
    assert "asr" not in stale  # rama hermana: no se toca
    assert "titlecard" not in stale


def test_roundtrip_serialization() -> None:
    g = _studio_graph()
    g.compute_fingerprints()
    restored = LineageGraph.from_dict(g.to_dict())
    assert restored.topo_order() == g.topo_order()
    assert {n.node_id for n in restored.nodes()} == {n.node_id for n in g.nodes()}


# --------------------------------------------------------------------------
# W04 — migración de esquema de ProductionState (checkpoints viejos reanudan)
# --------------------------------------------------------------------------


def test_v1_checkpoint_without_schema_version_migrates_to_v2() -> None:
    """Un checkpoint v1 (sin lineage ni schema_version) se reanuda sin romper."""
    from comfy_studio.orchestration.state import (
        SCHEMA_VERSION,
        ProductionState,
        migrate_state_dict,
    )

    old = {
        "id": "prod_old",
        "idea": "reel viejo",
        "phase": "executing",
        "sequences": [],
        # sin schema_version ni lineage: formato v1.
    }
    migrated = migrate_state_dict(old)
    assert migrated["schema_version"] == SCHEMA_VERSION
    assert migrated["lineage"] == {"nodes": []}
    # Carga completa: no lanza y conserva los datos del usuario.
    state = ProductionState.from_dict(old)
    assert state.id == "prod_old"
    assert state.idea == "reel viejo"
    assert state.phase == "executing"
    assert state.lineage == {"nodes": []}


def test_migration_is_idempotent_and_preserves_lineage() -> None:
    from comfy_studio.orchestration.state import migrate_state_dict

    current = {
        "schema_version": 2,
        "id": "p",
        "idea": "x",
        "sequences": [],
        "lineage": {"nodes": [{"node_id": "a", "kind": "brief"}]},
    }
    once = migrate_state_dict(current)
    twice = migrate_state_dict(once)
    assert once == twice  # idempotente
    assert once["lineage"]["nodes"][0]["node_id"] == "a"  # no descarta datos


def test_state_roundtrip_includes_schema_version_and_lineage() -> None:
    from comfy_studio.orchestration.state import SCHEMA_VERSION, ProductionState

    state = ProductionState(idea="dron costero")
    state.lineage = {"nodes": [{"node_id": "brief", "kind": "brief"}]}
    data = state.to_dict()
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["lineage"]["nodes"][0]["node_id"] == "brief"
    restored = ProductionState.from_dict(data)
    assert restored.lineage == state.lineage
