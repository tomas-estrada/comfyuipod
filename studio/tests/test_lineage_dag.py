"""DAG de producción con invalidación selectiva (plan Ultra, W04 / A05).

Verifica el objetivo concreto de la política: «cambiar un título no regenera
actores; cambiar iluminación no recalcula transcripción». Puro cálculo en CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.orchestration.lineage import LineageGraph, LineageNode, _hash_inputs


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
# W04 — `LineageGraph.descendants`, métodos vecinos y `_hash_inputs`
# --------------------------------------------------------------------------


def test_descendants_transitive_closure_and_excludes_self() -> None:
    """Cierre transitivo completo; en un DAG el nodo consultado no aparece."""
    g = _studio_graph()
    assert g.descendants("brief") == {"asr", "geometry", "render", "comp", "final"}
    assert g.descendants("lighting") == {"comp", "final"}
    assert g.descendants("title") == {"titlecard", "final"}
    assert g.descendants("geometry") == {"render", "comp", "final"}
    # Hoja: nadie cuelga de ella.
    assert g.descendants("final") == set()
    # Determinista: repetir la consulta devuelve el mismo conjunto.
    assert g.descendants("brief") == g.descendants("brief")


def test_descendants_on_empty_graph_unknown_and_isolated_nodes() -> None:
    """Grafo vacío, nodo inexistente y nodo aislado → conjunto vacío."""
    assert LineageGraph().descendants("cualquiera") == set()
    g = _studio_graph()
    assert g.descendants("no-existe") == set()
    g.add("aislado", "nota", inputs={})
    assert g.descendants("aislado") == set()


def test_descendants_diamond_returns_set_with_each_node_once() -> None:
    """Diamante a -> {b, c} -> e: cada descendiente aparece una vez (es un set)."""
    g = LineageGraph()
    g.add("a", "x")
    g.add("b", "x", depends_on=["a"])
    g.add("c", "x", depends_on=["a"])
    g.add("e", "x", depends_on=["b", "c"])
    result = g.descendants("a")
    assert isinstance(result, set)
    assert result == {"b", "c", "e"}
    assert g.descendants("b") == {"e"}


def test_descendants_tolerates_cycle_including_queried_node() -> None:
    """Comportamiento actual anclado (no se parchea): con un ciclo,
    `descendants` no lanza ni cuelga, pero el nodo consultado SÍ aparece en
    el resultado, aunque su docstring diga «sin incluirlo». `topo_order`
    sigue siendo la puerta que rechaza grafos cíclicos (ver
    test_cycle_is_detected)."""
    g = LineageGraph()
    g.add("a", "x", depends_on=["b"])
    g.add("b", "x", depends_on=["a"])
    assert g.descendants("a") == {"a", "b"}
    assert g.descendants("b") == {"a", "b"}


def test_hash_inputs_is_deterministic_and_order_independent() -> None:
    """Mismo contenido → mismo hash, sin importar el orden de inserción de
    las entradas ni el orden de los fingerprints de dependencias."""
    base = _hash_inputs("render", {"seed": 7, "width": 1920}, ["bbb", "aaa"])
    reordered = _hash_inputs("render", {"width": 1920, "seed": 7}, ["aaa", "bbb"])
    assert base == reordered
    assert base == _hash_inputs("render", {"seed": 7, "width": 1920}, ["bbb", "aaa"])


def test_hash_inputs_changes_with_kind_inputs_and_deps() -> None:
    """Sensibilidad: cambia con el tipo, con cualquier entrada y con las deps."""
    base = _hash_inputs("render", {"seed": 7}, ["d1"])
    assert _hash_inputs("title", {"seed": 7}, ["d1"]) != base  # kind
    assert _hash_inputs("render", {"seed": 8}, ["d1"]) != base  # valor
    assert _hash_inputs("render", {"seed": 7, "w": 1}, ["d1"]) != base  # alta
    assert _hash_inputs("render", {"seed": 7}, ["d2"]) != base  # dep
    assert _hash_inputs("render", {"seed": 7}, ["d1", "d2"]) != base  # nº deps


def test_hash_inputs_is_16_hex_and_handles_unserializable_values() -> None:
    """Formato: 16 hex minúsculas. `default=str` cubre valores no JSON de
    forma estable (objeto con `__str__` determinista → mismo hash)."""

    class Thing:
        def __str__(self) -> str:
            return "thing"

    h = _hash_inputs("x", {"obj": Thing()}, [])
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)
    assert h == _hash_inputs("x", {"obj": Thing()}, [])


def test_hash_inputs_golden_vector_anchors_the_algorithm() -> None:
    """Vector fijo: si cambia el algoritmo, TODOS los fingerprints cambian y
    `plan_recompute` marcaría stale un checkpoint entero. Cambiar este valor
    debe ser una decisión consciente con su propia migración."""
    assert _hash_inputs("render", {"seed": 7}, ["d1"]) == "b801b4c48ab81675"
    assert (
        _hash_inputs("brief", {"text": "dron costero atardecer"}, [])
        == "1f8f4e2351ae3e53"
    )


def test_get_returns_none_for_unknown_node() -> None:
    g = _studio_graph()
    assert g.get("brief") is not None
    assert g.get("no-existe") is None
    assert LineageGraph().get("brief") is None


def test_set_computed_without_prior_fingerprints_computes_them() -> None:
    """Rama `fingerprint is None`: `set_computed` dispara `compute_fingerprints`
    para no fijar un artifact con fingerprint desconocido."""
    g = LineageGraph()
    g.add("a", "brief", inputs={"t": 1})
    g.set_computed("a", "/out/a.bin")
    node = g.get("a")
    assert node.artifact == "/out/a.bin"
    assert node.fingerprint is not None


def test_plan_recompute_marks_added_nodes_and_ignores_removed_ones() -> None:
    """Nodo nuevo tras el baseline → stale (no estaba en previous); ids en
    previous que ya no existen en el grafo → ignorados."""
    g = LineageGraph()
    g.add("a", "brief", inputs={"t": 1})
    baseline = g.compute_fingerprints()
    g.add("b", "render", inputs={}, depends_on=["a"])
    assert g.plan_recompute(baseline) == {"b"}
    # Alta de una entrada nueva en un nodo existente también lo marca stale.
    g.update_inputs("a", extra=True)
    assert g.plan_recompute(baseline) == {"a", "b"}
    # Un id en previous que ya no está en el grafo no aparece en el plan.
    ghost_baseline = dict(baseline)
    ghost_baseline["borrado"] = "0" * 16
    assert g.plan_recompute(ghost_baseline) == {"a", "b"}


def test_invalidate_artifacts_skips_unknown_ids() -> None:
    """Ids stale que no corresponden a ningún nodo se ignoran sin lanzar."""
    g = _studio_graph()
    g.compute_fingerprints()
    g.set_computed("brief", "/out/brief.bin")
    g.set_computed("asr", "/out/asr.bin")
    g.invalidate_artifacts({"brief", "ghost"})
    assert g.get("brief").artifact is None  # sí se borró
    assert g.get("asr").artifact == "/out/asr.bin"  # conservado
    assert g.get("ghost") is None


def test_serialization_preserves_fingerprint_and_artifact() -> None:
    """Roundtrip con artifact/fingerprint reales + `from_dict` sin clave."""
    g = _studio_graph()
    g.compute_fingerprints()
    g.set_computed("brief", "/out/brief.bin")
    restored = LineageGraph.from_dict(g.to_dict())
    assert restored.get("brief").artifact == "/out/brief.bin"
    assert restored.get("brief").fingerprint == g.get("brief").fingerprint
    assert restored.get("asr").fingerprint is not None
    # `from_dict` tolera datos sin clave "nodes" → grafo vacío.
    empty = LineageGraph.from_dict({})
    assert empty.nodes() == []
    assert empty.topo_order() == []


def test_lineage_node_from_dict_without_optional_fields_uses_defaults() -> None:
    """Defaults de `LineageNode.from_dict`: solo node_id/kind son obligatorios."""
    node = LineageNode.from_dict({"node_id": "a", "kind": "brief"})
    assert node.inputs == {}
    assert node.depends_on == []
    assert node.fingerprint is None
    assert node.artifact is None


def test_add_replaces_node_with_same_id() -> None:
    """Comportamiento actual: volver a añadir un id lo sobrescribe en silencio."""
    g = LineageGraph()
    g.add("a", "brief", inputs={"t": 1})
    replaced = g.add("a", "render", inputs={"t": 2})
    assert replaced.kind == "render"
    assert len(g.nodes()) == 1
    assert g.get("a").inputs == {"t": 2}


def test_topo_order_is_deterministic_exact_sequence() -> None:
    """`ready.sort()` hace el orden estable: el mismo grafo → la misma lista."""
    assert _studio_graph().topo_order() == [
        "brief",
        "asr",
        "geometry",
        "lighting",
        "render",
        "comp",
        "title",
        "titlecard",
        "final",
    ]


def test_dangling_dependency_is_ignored_without_crashing() -> None:
    """Dependencia colgante (id que no existe como nodo): se ignora en el
    fingerprint y en el orden topológico; el grafo sigue siendo utilizable."""
    g = LineageGraph()
    g.add("huerfano", "render", depends_on=["borrado"])
    fps = g.compute_fingerprints()
    assert set(fps) == {"huerfano"}
    # Sin dep real que incorporar: el hash equivale a un nodo sin dependencias.
    assert fps["huerfano"] == _hash_inputs("render", {}, [])
    assert g.topo_order() == ["huerfano"]
    # El id colgante no está en el grafo, pero los nodos que lo declaran
    # siguen siendo sus dependientes (semántica de la arista invertida).
    assert g.descendants("borrado") == {"huerfano"}


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
