"""Memoria espacial, multishot y 4D (plan Ultra, W17 / A19).

A->B->A conserva anclas y registra drift; imagen generada no es evidencia del
atlas; límites de contexto. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.spatial_memory import (
    Anchor,
    Provenance,
    SpatialError,
    SpatialMemory,
    assert_not_atlas_evidence,
)


def test_round_trip_conserves_anchor_with_zero_drift() -> None:
    """A19: A->B->A vuelve al mismo sitio -> drift 0, returned True."""
    m = SpatialMemory()
    m.register(Anchor("puente_4sur"))
    m.visit("puente_4sur", "A", (10.0, 0.0, 5.0))
    m.visit("puente_4sur", "B", (20.0, 0.0, 5.0))
    m.visit("puente_4sur", "A2", (10.0, 0.0, 5.0))  # vuelve a A
    rep = m.round_trip_drift("puente_4sur")
    assert rep["drift"] == 0.0
    assert rep["returned"] is True
    assert rep["shots"] == ["A", "B", "A2"]


def test_drift_is_measured_when_anchor_moves() -> None:
    """A19: se registra el drift (no se oculta)."""
    m = SpatialMemory()
    m.register(Anchor("farola"))
    m.visit("farola", "A", (0.0, 0.0, 0.0))
    m.visit("farola", "B", (1.0, 0.0, 0.0))
    m.visit("farola", "A2", (0.3, 0.0, 0.0))  # no vuelve exacto
    rep = m.round_trip_drift("farola")
    assert rep["drift"] == pytest.approx(0.3)
    assert rep["returned"] is False


def test_visit_unregistered_anchor_rejected() -> None:
    m = SpatialMemory()
    with pytest.raises(SpatialError, match="no registrada"):
        m.visit("x", "A", (0.0, 0.0, 0.0))


def test_context_limit_respected() -> None:
    """A19: límites de escena/duración/contexto por receta se respetan."""
    m = SpatialMemory(max_shots=2)
    m.register(Anchor("a"))
    m.visit("a", "s1", (0, 0, 0))
    m.visit("a", "s2", (0, 0, 0))
    with pytest.raises(SpatialError, match="límite de contexto"):
        m.visit("a", "s3", (0, 0, 0))


def test_project_anchor_count_limit_rejects_new_anchors_without_mutation() -> None:
    m = SpatialMemory(max_anchors=2)
    m.register(Anchor("a"))
    m.register(Anchor("b"))

    with pytest.raises(SpatialError, match="límite de anclas.*2"):
        m.register(Anchor("c"))

    assert set(m.anchors) == {"a", "b"}


def test_spatial_memory_rejects_unbounded_anchor_and_shot_ids() -> None:
    with pytest.raises(SpatialError, match="anchor_id.*128"):
        SpatialMemory().register(Anchor("a" * 129))

    memory = SpatialMemory()
    memory.register(Anchor("a"))
    with pytest.raises(SpatialError, match="shot_id.*128"):
        memory.visit("a", "s" * 129, (0, 0, 0))


def test_project_total_visit_limit_is_shared_across_anchors_and_idempotent() -> None:
    m = SpatialMemory(max_shots=4, max_total_visits=3)
    m.register(Anchor("a"))
    m.register(Anchor("b"))
    m.visit("a", "s1", (0, 0, 0))
    m.visit("a", "s2", (0, 0, 0))
    m.visit("b", "s3", (0, 0, 0))

    # Retrying an existing visit at the project limit remains a no-op.
    m.visit("a", "s1", (0, 0, 0))
    with pytest.raises(SpatialError, match="límite total de visitas.*3"):
        m.visit("b", "s4", (0, 0, 0))

    assert sum(len(visits) for visits in m.visits.values()) == 3
    assert [visit.shot_id for visit in m.visits["b"]] == ["s3"]


def test_project_wide_limits_survive_serialization() -> None:
    memory = SpatialMemory(max_anchors=2, max_total_visits=3)
    memory.register(Anchor("a"))
    memory.register(Anchor("b"))
    memory.visit("a", "s1", (0, 0, 0))
    memory.visit("b", "s2", (0, 0, 0))
    memory.visit("a", "s3", (0, 0, 0))
    restored = SpatialMemory.from_dict(memory.to_dict())

    with pytest.raises(SpatialError, match="límite de anclas.*2"):
        restored.register(Anchor("c"))
    with pytest.raises(SpatialError, match="límite total de visitas.*3"):
        restored.visit("b", "s4", (0, 0, 0))


def test_round_trip_needs_three_visits() -> None:
    m = SpatialMemory()
    m.register(Anchor("a"))
    m.visit("a", "A", (0, 0, 0))
    m.visit("a", "B", (1, 0, 0))
    with pytest.raises(SpatialError, match="A->B->A"):
        m.round_trip_drift("a")


def test_inferred_anchor_is_not_atlas_evidence() -> None:
    """A19: ninguna imagen/ancla generada se promociona a evidencia del atlas."""
    m = SpatialMemory()
    m.register(Anchor("fachada_sintetica", provenance=Provenance.INFERRED))
    with pytest.raises(SpatialError, match="no es evidencia del atlas"):
        assert_not_atlas_evidence(m, "fachada_sintetica")


def test_synthetic_anchor_is_not_atlas_evidence() -> None:
    m = SpatialMemory()
    m.register(Anchor("ancla_fixture", provenance=Provenance.SYNTHETIC))
    with pytest.raises(SpatialError, match="no es evidencia del atlas"):
        assert_not_atlas_evidence(m, "ancla_fixture")


def test_observed_anchor_is_valid_atlas_evidence() -> None:
    m = SpatialMemory()
    m.register(Anchor("puente_real", provenance=Provenance.OBSERVED))
    assert_not_atlas_evidence(m, "puente_real")  # no lanza


def test_serialization_reports_drift_per_anchor() -> None:
    m = SpatialMemory()
    m.register(Anchor("a"))
    m.visit("a", "A", (0, 0, 0))
    m.visit("a", "B", (2, 0, 0))
    d = m.to_dict()
    assert d["drift"]["a"] == pytest.approx(2.0)
    assert d["anchors"]["a"]["provenance"] == "observed"
