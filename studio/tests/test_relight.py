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
    assert pixels == bytes((255, 0, 0, 200, 0, 128, 0, 255, 0, 0, 64, 255, 0, 0, 0, 128))


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


# --- C21: relight CPU acotado y determinista con AOV sintéticos calibrados ---


def _light_dir(azimuth_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """Dirección de luz unitaria (misma convención documentada en el módulo)."""
    import math

    azimuth = math.radians(azimuth_deg)
    elevation = math.radians(elevation_deg)
    return (
        math.cos(elevation) * math.sin(azimuth),
        math.sin(elevation),
        math.cos(elevation) * math.cos(azimuth),
    )


def _ref_render(
    albedo: bytes,
    normal: bytes,
    occlusion: bytes,
    light: dict[str, float],
    width: int,
    height: int,
) -> bytes:
    """Render Lambertiano de referencia INDEPENDIENTE del módulo (ground truth)."""
    import math

    lx, ly, lz = _light_dir(light["azimuth_deg"], light["elevation_deg"])
    pixels = width * height
    out = bytearray(pixels * 3)
    for index in range(pixels):
        nx = normal[index * 3] / 127.5 - 1
        ny = normal[index * 3 + 1] / 127.5 - 1
        nz = normal[index * 3 + 2] / 127.5 - 1
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        nx, ny, nz = nx / length, ny / length, nz / length
        diffuse = max(0.0, nx * lx + ny * ly + nz * lz)
        irradiance = diffuse * light["intensity"] * (1 - occlusion[index] / 255) + light["ambient"]
        for channel in range(3):
            value = round(albedo[index * 3 + channel] * irradiance)
            out[index * 3 + channel] = 0 if value < 0 else (255 if value > 255 else value)
    return bytes(out)


def _synthetic_scene(width: int = 32, height: int = 24) -> dict[str, bytes]:
    """Escena sintética: albedo en gradiente, normales de plano inclinado,
    sombra proyectada dura+suave y un logo protegido cuadrado."""
    pixels = width * height
    albedo = bytearray(pixels * 3)
    normal = bytearray(pixels * 3)
    occlusion = bytearray(pixels)
    protected = bytearray(pixels)
    for y in range(height):
        for x in range(width):
            index = y * width + x
            albedo[index * 3] = 40 + x * 6
            albedo[index * 3 + 1] = 30 + y * 7
            albedo[index * 3 + 2] = 90
            # plano inclinado constante: n = normalize(0.3, -0.4, 0.8)
            nx, ny, nz = 0.3, -0.4, 0.8
            length = (nx * nx + ny * ny + nz * nz) ** 0.5
            normal[index * 3] = round(127.5 * (nx / length) + 127.5)
            normal[index * 3 + 1] = round(127.5 * (ny / length) + 127.5)
            normal[index * 3 + 2] = round(127.5 * (nz / length) + 127.5)
            # sombra proyectada: bloque duro + banda de penumbra.
            if 18 <= x < 28 and 4 <= y < 18:
                occlusion[index] = 255
            elif 17 <= x < 18 and 4 <= y < 18:
                occlusion[index] = 128
            # logo protegido.
            if 2 <= x < 8 and 2 <= y < 8:
                protected[index] = 255
    return {
        "albedo": bytes(albedo),
        "normal": bytes(normal),
        "occlusion": bytes(occlusion),
        "protected": bytes(protected),
    }


def _frame_aovs(scene: dict[str, bytes], source_frame: bytes):
    from comfy_studio.relight import FrameAOVs

    return FrameAOVs(
        albedo=scene["albedo"],
        normal=scene["normal"],
        occlusion=scene["occlusion"],
        protected=scene["protected"],
        source_frame=source_frame,
    )


_SOURCE_LIGHT = {"azimuth_deg": -30.0, "elevation_deg": 40.0, "intensity": 1.0, "ambient": 0.1}
_TARGET_LIGHTS = [
    {"azimuth_deg": 25.0, "elevation_deg": 55.0, "intensity": 1.4, "ambient": 0.2},
    {"azimuth_deg": 70.0, "elevation_deg": 30.0, "intensity": 0.8, "ambient": 0.15},
    {"azimuth_deg": 25.0, "elevation_deg": 55.0, "intensity": 1.6, "ambient": 0.3},
    {"azimuth_deg": 110.0, "elevation_deg": 20.0, "intensity": 1.2, "ambient": 0.05},
]


def _synthetic_inputs(width: int = 32, height: int = 24):
    scene = _synthetic_scene(width, height)
    source = _ref_render(
        scene["albedo"], scene["normal"], scene["occlusion"], _SOURCE_LIGHT, width, height
    )
    ground_truth = [
        _ref_render(scene["albedo"], scene["normal"], scene["occlusion"], light, width, height)
        for light in _TARGET_LIGHTS
    ]
    frames = [_frame_aovs(scene, source)]
    return scene, source, ground_truth, frames


def test_synthetic_aov_relight_matches_independent_ground_truth() -> None:
    """A18/C21: relight CPU con AOV calibrados reproduce el ground truth sintético
    conocido; reporta error por frame, temporal y de región no editada."""
    from comfy_studio.relight import LightParams, relight_frames_synthetic

    scene, source, ground_truth, frames = _synthetic_inputs()
    targets = [LightParams(**light) for light in _TARGET_LIGHTS]
    source_light = LightParams(**_SOURCE_LIGHT)
    report = relight_frames_synthetic(
        frames, 32, 24, source_light, targets, ground_truth, error_tolerance=2.0
    )
    assert report["renderer"] == "cpu-lambert-aov-relight-v1"
    assert report["frames"] == 4 and len(report["frames_rgb"]) == 4
    # calibración: los AOV explican el frame fuente bajo la luz declarada.
    assert report["calibration"]["ok"] is True
    assert report["calibration"]["mae"] == 0.0
    # error por frame contra el ground truth independiente.
    assert report["edited_region"]["within_tolerance"] is True
    assert report["edited_region"]["max_abs"] <= 2.0
    for entry in report["per_frame"]:
        assert entry["edited_mae_vs_ground_truth"] <= 2.0
        assert entry["edited_max_abs"] <= 2.0
    # la iluminación cambió de verdad en la región editable.
    assert report["edited_region"]["mae_vs_source"] > 0.5
    # región no editada: logo preservado bit a bit contra el frame fuente,
    # y el ground truth sintético también conserva el logo.
    assert report["protected_region"]["bit_exact"] is True
    assert report["protected_region"]["max_abs_diff_vs_source"] == 0.0
    assert report["protected_region"]["mae_vs_ground_truth"] <= 2.0
    # consistencia temporal: deltas entre frames iguales a los del ground truth.
    assert report["temporal"]["within_tolerance"] is True
    assert report["temporal"]["max_delta_error"] <= 2.0
    assert report["passed"] is True


def test_synthetic_aov_relight_detects_wrong_light_against_ground_truth() -> None:
    """Un parámetro de luz equivocado NO pasa la comparación contra el ground truth."""
    from comfy_studio.relight import LightParams, relight_frames_synthetic

    scene, source, ground_truth, frames = _synthetic_inputs()
    targets = [
        LightParams(azimuth_deg=175.0, elevation_deg=-60.0, intensity=3.5, ambient=0.9),
        LightParams(azimuth_deg=175.0, elevation_deg=-60.0, intensity=3.5, ambient=0.9),
        LightParams(azimuth_deg=175.0, elevation_deg=-60.0, intensity=3.5, ambient=0.9),
        LightParams(azimuth_deg=175.0, elevation_deg=-60.0, intensity=3.5, ambient=0.9),
    ]
    report = relight_frames_synthetic(
        frames,
        32,
        24,
        LightParams(**_SOURCE_LIGHT),
        targets,
        ground_truth,
        error_tolerance=2.0,
    )
    assert report["passed"] is False
    assert report["edited_region"]["within_tolerance"] is False
    assert report["edited_region"]["mae_vs_ground_truth"] > 2.0


def test_synthetic_aov_relight_preserves_protected_logo_pixels_bit_exact() -> None:
    """Los píxeles del logo protegido quedan idénticos al frame fuente aunque la
    luz declarada los cambiaría."""
    from comfy_studio.relight import LightParams, relight_frames_synthetic

    scene, source, ground_truth, frames = _synthetic_inputs()
    targets = [LightParams(**light) for light in _TARGET_LIGHTS]
    report = relight_frames_synthetic(
        frames, 32, 24, LightParams(**_SOURCE_LIGHT), targets, ground_truth
    )
    width, height = 32, 24
    for out_frame in report["frames_rgb"]:
        for y in range(height):
            for x in range(width):
                if scene["protected"][y * width + x] >= 128:
                    offset = (y * width + x) * 3
                    assert out_frame[offset : offset + 3] == source[offset : offset + 3]


def test_synthetic_aov_relight_rejects_uncalibrated_aovs() -> None:
    """AOVs no calibrados (el frame fuente no se deriva de los AOV y la luz
    declarada) se rechazan: la comparación no tendría sentido."""
    import pytest

    from comfy_studio.relight import LightParams, RelightError, relight_frames_synthetic

    scene, source, ground_truth, frames = _synthetic_inputs()
    corrupted = bytearray(source)
    corrupted[0 : 32 * 3] = b"\xff" * (32 * 3)
    bad_frames = [_frame_aovs(scene, bytes(corrupted))]
    with pytest.raises(RelightError, match="calibración"):
        relight_frames_synthetic(
            bad_frames,
            32,
            24,
            LightParams(**_SOURCE_LIGHT),
            [LightParams(**light) for light in _TARGET_LIGHTS],
            ground_truth,
        )


def test_synthetic_aov_relight_validates_inputs() -> None:
    from comfy_studio.relight import FrameAOVs, LightParams, RelightError, relight_frames_synthetic

    scene, source, ground_truth, frames = _synthetic_inputs()
    source_light = LightParams(**_SOURCE_LIGHT)
    targets = [LightParams(**light) for light in _TARGET_LIGHTS]
    # luz objetivo igual a la fuente: no cambia iluminación.
    with pytest.raises(RelightError, match="iluminación"):
        relight_frames_synthetic(frames, 32, 24, source_light, [source_light] * 4, ground_truth)
    # número de frames distinto al de luces objetivo.
    with pytest.raises(RelightError, match="luz objetivo"):
        relight_frames_synthetic(frames, 32, 24, source_light, targets[:2], ground_truth)
    # ground truth con longitud distinta.
    with pytest.raises(RelightError, match="ground truth"):
        relight_frames_synthetic(frames, 32, 24, source_light, targets, ground_truth[:2])
    # buffers con longitud incorrecta.
    with pytest.raises(RelightError):
        relight_frames_synthetic(
            [_frame_aovs(scene, source[:-1])], 32, 24, source_light, targets[:1], ground_truth[:1]
        )
    with pytest.raises(RelightError):
        bad_albedo = FrameAOVs(
            albedo=scene["albedo"][:-1],
            normal=scene["normal"],
            occlusion=scene["occlusion"],
            protected=scene["protected"],
            source_frame=source,
        )
        relight_frames_synthetic(bad_albedo, 32, 24, source_light, targets[:1], ground_truth[:1])
    # normal nula.
    flat_normal = bytearray(scene["normal"])
    flat_normal[0:3] = b"\x80\x80\x80"
    flat_scene = {**scene, "normal": bytes(flat_normal)}
    with pytest.raises(RelightError, match="normal"):
        relight_frames_synthetic(
            [_frame_aovs(flat_scene, source)], 32, 24, source_light, targets[:1], ground_truth[:1]
        )
    # límites de frames/píxeles.
    many = [_frame_aovs(scene, source)] * 65
    with pytest.raises(RelightError, match="frames"):
        relight_frames_synthetic(many, 32, 24, source_light, targets, ground_truth)
    big = [_frame_aovs(scene, source)]
    with pytest.raises(RelightError, match="px"):
        relight_frames_synthetic(big, 2000, 2000, source_light, targets[:1], ground_truth[:1])


def test_light_params_bounds_are_enforced() -> None:
    from comfy_studio.relight import LightParams, RelightError

    LightParams(azimuth_deg=0, elevation_deg=0, intensity=1, ambient=0)  # no lanza
    with pytest.raises(RelightError):
        LightParams(azimuth_deg=181, elevation_deg=0, intensity=1, ambient=0)
    with pytest.raises(RelightError):
        LightParams(azimuth_deg=0, elevation_deg=91, intensity=1, ambient=0)
    with pytest.raises(RelightError):
        LightParams(azimuth_deg=0, elevation_deg=0, intensity=4.5, ambient=0)
    with pytest.raises(RelightError):
        LightParams(azimuth_deg=0, elevation_deg=0, intensity=1, ambient=1.5)
    with pytest.raises(RelightError):
        LightParams(azimuth_deg=0, elevation_deg=0, intensity=-0.1, ambient=0)


def test_temporal_relight_curve_requires_ordered_bounded_frame_points() -> None:
    import math

    from comfy_studio.relight import _curve_values, _validate_light_curve

    points = _validate_light_curve([{"frame": 0, "gain": 0.5}, {"frame": 2, "gain": 1.5}], 4)
    assert _curve_values(points, 4) == [0.5, 1.0, 1.5, 1.5]
    for invalid in (
        [{"frame": 1, "gain": 1}, {"frame": 3, "gain": 2}],
        [{"frame": 0, "gain": 1}, {"frame": 0, "gain": 2}],
        [{"frame": 0, "gain": 1}, {"frame": 4, "gain": 2}],
        [{"frame": 0, "gain": 1}, {"frame": 3, "gain": 1}],
        [{"frame": 0, "gain": 1}, {"frame": 3, "gain": math.inf}],
        [{"frame": False, "gain": 1}, {"frame": 3, "gain": 2}],
    ):
        with pytest.raises(RelightError):
            _validate_light_curve(invalid, 4)
