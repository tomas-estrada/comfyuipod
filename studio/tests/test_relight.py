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


def _decode_relight_png(png: bytes) -> tuple[int, int, int, bytes]:
    """Parser PNG mínimo (stdlib) para verificar píxeles y tipo de color."""

    import struct
    import zlib

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    color_type: int | None = None
    width = height = None
    idat = b""
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        payload = png[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, _bit, color_type, *_rest = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            idat += payload
        offset += 12 + length
    assert width is not None and height is not None and color_type is not None
    raw = zlib.decompress(idat)
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    pixels = b"".join(
        raw[row * (stride + 1) + 1 : (row + 1) * (stride + 1)] for row in range(height)
    )
    return width, height, color_type, pixels


def test_render_relight_composites_shading_and_preserves_alpha() -> None:
    import hashlib

    from comfy_studio.relight import render_relight

    albedo = bytes((255, 0, 0, 200, 0, 255, 0, 255, 0, 0, 255, 255, 64, 64, 64, 128))
    shading = bytes((255, 128, 64, 0))
    first = render_relight(albedo, shading, 2, 2)
    second = render_relight(albedo, shading, 2, 2)
    assert first["png"] == second["png"]  # codificación determinista
    assert first["sha256"] == hashlib.sha256(first["png"]).hexdigest()
    assert first["albedo_sha256"] == hashlib.sha256(albedo).hexdigest()
    assert first["shading_sha256"] == hashlib.sha256(shading).hexdigest()
    width, height, color_type, pixels = _decode_relight_png(first["png"])
    assert (width, height, color_type) == (2, 2, 6)
    # lit = albedo × shading/255; el alfa del albedo queda intacto.
    assert pixels == bytes(
        (255, 0, 0, 200, 0, 128, 0, 255, 0, 0, 64, 255, 0, 0, 0, 128)
    )


def test_render_relight_rejects_out_of_bounds_inputs() -> None:
    import math

    from comfy_studio.relight import RelightError, render_relight

    albedo = b"\0" * 12  # RGB 2×2
    shading = b"\0" * 4
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 0, 2)
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 4097, 1)
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 4096, 4096)  # 16.7M px > 4M
    with pytest.raises(RelightError):
        render_relight(albedo[:-1], shading, 2, 2)  # albedo incompleto
    with pytest.raises(RelightError):
        render_relight(albedo, shading[:-1], 2, 2)  # shading incompleto
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 2, 2, intensity=4.5)
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 2, 2, ambient=1.5)
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 2, 2, intensity=math.nan)
    with pytest.raises(RelightError):
        render_relight(albedo, shading, 2, 2, ambient=math.inf)
