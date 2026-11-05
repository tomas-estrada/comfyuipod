"""Inspiración activa → receta editable (plan Ultra, W06 / A09).

Análisis local sobre la biblioteca propia; conectores opcionales/blocked, nunca
de pago. Distingue observado/inferido/adaptado; no promete viralidad. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.inspiration import (
    ConnectorStatus,
    Cut,
    Origin,
    ReferenceAnalysis,
    assert_no_paid_connector,
    connector_catalog,
    derive_recipe,
)


def _analysis() -> ReferenceAnalysis:
    return ReferenceAnalysis(
        reference_id="ref1",
        cuts=[Cut(0.0, 2.0), Cut(2.0, 3.5), Cut(3.5, 6.0)],
        beats=[0.0, 0.5, 1.0, 1.5, 2.0],
        titles=[{"text": "ATARDECER", "start_s": 0.2, "origin": "observed"}],
        camera_moves=["pan", "static", "zoom"],
        source="own_library",
    )


def test_analysis_computes_beat_tempo() -> None:
    a = _analysis()
    assert a.beat_interval_s() == 0.5  # beats cada 0.5 s


def test_derive_recipe_maps_cuts_titles_and_beats() -> None:
    recipe = derive_recipe(_analysis())
    kinds = [s.kind for s in recipe.steps]
    assert kinds.count("shot") == 3  # un shot por corte
    assert "beat_sync" in kinds       # tempo inferido
    assert "title" in kinds           # título OCR observado
    # Los shots son observados; el beat_sync es inferido.
    assert all(s.origin is Origin.OBSERVED for s in recipe.steps if s.kind == "shot")
    assert all(s.origin is Origin.INFERRED for s in recipe.steps if s.kind == "beat_sync")


def test_recipe_distinguishes_observed_inferred_adapted() -> None:
    """A09: el análisis distingue qué observó, infirió y adaptó al brief."""
    recipe = derive_recipe(_analysis(), brief_shots=5)
    summary = recipe.to_dict()["origin_summary"]
    assert summary["observed"] >= 3   # 3 shots + título
    assert summary["inferred"] >= 1   # beat_sync
    assert summary["adapted"] == 1    # adaptación de 3 -> 5 shots
    # La adaptación deja nota trazable.
    assert "adaptado" in recipe.notes


def test_recipe_does_not_promise_virality() -> None:
    recipe = derive_recipe(_analysis())
    assert "no predice viralidad" in recipe.to_dict()["disclaimer"]


def test_connector_without_credentials_is_blocked_not_invented() -> None:
    """A09: un conector sin credenciales queda blocked, no inventa datos."""
    cats = {c.name: c for c in connector_catalog(env={})}
    assert cats["own_library"].status is ConnectorStatus.AVAILABLE
    assert cats["wikimedia_commons"].status is ConnectorStatus.BLOCKED
    assert not cats["wikimedia_commons"].is_usable()


def test_optional_connector_enabled_by_env() -> None:
    cats = {c.name: c for c in connector_catalog(env={"STUDIO_WIKIMEDIA_ENABLED": "1"})}
    assert cats["wikimedia_commons"].status is ConnectorStatus.AVAILABLE


def test_paid_connector_is_never_invoked() -> None:
    """A09: no se invoca cloud/compra ni cuenta no concedida."""
    cats = {c.name: c for c in connector_catalog(env={})}
    heygen = cats["heygen_reverse"]
    assert heygen.status is ConnectorStatus.FORBIDDEN_PAID
    with pytest.raises(RuntimeError, match="no se invoca"):
        assert_no_paid_connector(heygen)


def test_offline_mode_works_on_own_library() -> None:
    """Modo sin red: el análisis funciona solo con la biblioteca propia."""
    a = ReferenceAnalysis(reference_id="local", cuts=[Cut(0.0, 1.0)], source="own_library")
    recipe = derive_recipe(a)
    assert recipe.reference_id == "local"
    assert len([s for s in recipe.steps if s.kind == "shot"]) == 1
    # No requirió ningún conector externo.
    assert a.source == "own_library"
