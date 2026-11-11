"""Reiluminación, materiales y restilización (plan Ultra, W16 / A18).

Mapeo AOV con validez/unknown; relight no invalida campos ajenos; relight y
restyle son tareas distintas. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.relight import (
    AOVSet,
    AOVValidity,
    RelightError,
    TaskKind,
    assert_tasks_distinct,
    invalidate_for_task,
    preserved_fields,
)


def _aovset() -> AOVSet:
    s = AOVSet()
    for name in ("albedo", "normal", "depth", "roughness", "id", "shading", "shadow"):
        s.add(name)
    return s


def test_unknown_aov_is_not_used_as_valid() -> None:
    """A18: validez/unknown explícitos; un unknown no se da por bueno."""
    s = AOVSet()
    s.add("depth", validity=AOVValidity.UNKNOWN)
    assert "depth" in s.unknown_names()
    with pytest.raises(RelightError, match="unknown"):
        s.require_valid("depth")


def test_relight_only_invalidates_lighting_dependent_fields() -> None:
    """A18: recompilar una luz no invalida campos ajenos a esa intervención."""
    s = _aovset()
    to_recompute = invalidate_for_task(s, TaskKind.RELIGHT)
    # Se recomputan shading/shadow (dependen de la luz).
    assert "shading" in to_recompute and "shadow" in to_recompute
    # NO se tocan geometría ni identidad.
    assert "normal" not in to_recompute
    assert "depth" not in to_recompute
    assert "albedo" not in to_recompute
    assert "id" not in to_recompute


def test_relight_preserves_geometry_and_identity() -> None:
    s = _aovset()
    preserved = preserved_fields(s, TaskKind.RELIGHT)
    assert {"normal", "depth", "albedo", "roughness", "id"}.issubset(preserved)


def test_restyle_is_a_distinct_task_from_relight() -> None:
    """A18: restilización y reiluminación son tareas distintas."""
    assert_tasks_distinct(TaskKind.RELIGHT, TaskKind.RESTYLE)  # no lanza
    with pytest.raises(RelightError, match="tareas distintas"):
        assert_tasks_distinct(TaskKind.RELIGHT, TaskKind.RELIGHT)


def test_restyle_touches_appearance_but_not_geometry() -> None:
    s = _aovset()
    to_recompute = invalidate_for_task(s, TaskKind.RESTYLE)
    # Restyle afecta albedo/roughness (apariencia) y shading.
    assert "albedo" in to_recompute
    # Pero NO geometría pura.
    assert "normal" not in to_recompute
    assert "depth" not in to_recompute
    assert "id" not in to_recompute


def test_require_valid_passes_for_valid_aov() -> None:
    s = _aovset()
    s.require_valid("albedo")  # no lanza


def test_missing_aov_rejected() -> None:
    s = _aovset()
    with pytest.raises(RelightError, match="ausente"):
        s.require_valid("no_existe")


def test_aovset_serialization_splits_valid_unknown() -> None:
    s = AOVSet()
    s.add("albedo", AOVValidity.VALID)
    s.add("depth", AOVValidity.UNKNOWN)
    d = s.to_dict()
    assert d["valid"] == ["albedo"]
    assert d["unknown"] == ["depth"]
    assert AOVSet.from_dict(d).to_dict() == d


def test_invalid_aov_serialization_is_rejected() -> None:
    with pytest.raises(RelightError, match="schema desconocido"):
        AOVSet.from_dict({"schema": "aov-set/2", "aovs": {}})
    with pytest.raises(RelightError, match="validez de AOV inválida"):
        AOVSet().add("depth", "stale")  # type: ignore[arg-type]
