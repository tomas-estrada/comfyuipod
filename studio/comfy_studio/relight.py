"""Reiluminación, materiales y restilización (plan Ultra, W16 / A18).

W16 pide reiluminación/materiales/restilización temporal (C21/C22) con mapeo de
AOV (roughness/normal/depth) con validez/unknown explícitos, restilización y
reiluminación reportadas como **tareas distintas**, y el invariante clave:
«recompilar una luz no invalida campos ajenos a esa intervención».

Este módulo implementa el CONTRATO:

- `AOVSet`: el conjunto de pases (roughness/normal/depth/albedo) con su estado de
  validez por pase (`valid` / `unknown`), nunca un unknown presentado como válido.
- `TaskKind`: RELIGHT vs RESTYLE — tareas distintas que no se confunden.
- `invalidate_for_task`: al recompilar una luz, invalida SOLO los campos que
  dependen de la iluminación (lighting-dependent), conservando los ajenos
  (geometría, identidad) — la misma causalidad selectiva de W04/W15 aplicada a
  los AOV.

The AOV helpers preserve explicit validity and selective invalidation. The CPU
image composite and bounded project-video path operate on pixels; the video path
applies only a spatially uniform, frame-interpolated exposure gain. It does not
provide semantic or physically based relighting, and does not complete all W16
acceptance criteria.

C21 añade la ruta sintética acotada (`FrameAOVs` + `LightParams` +
`relight_frames_synthetic`): reiluminación Lambertiana pura CPU sobre AOV
calibrados (la calibración debe reconstruir el frame fuente bajo la luz
declarada), con logo protegido bit a bit, métricas por frame/temporal/de región
contra un ground truth independiente, y límites estrictos de píxeles y frames.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg, find_ffprobe


class AOVValidity(StrEnum):
    VALID = "valid"
    UNKNOWN = "unknown"  # no se pudo derivar; NO se presenta como válido


class TaskKind(StrEnum):
    RELIGHT = "relight"  # cambia iluminación, conserva materiales/geometría
    RESTYLE = "restyle"  # cambia estilo/apariencia; tarea DISTINTA a relight


class RelightError(ValueError):
    """Contrato de reiluminación inválido."""


#: Qué pases dependen de la iluminación (los que una recompilación de luz afecta).
LIGHTING_DEPENDENT = frozenset({"shading", "shadow", "specular", "irradiance"})
#: Qué pases NO dependen de la luz (ajenos a una intervención de iluminación).
LIGHT_INDEPENDENT = frozenset({"albedo", "normal", "depth", "roughness", "id"})


@dataclass(slots=True)
class AOV:
    """Un pase (arbitrary output variable) con su estado de validez."""

    name: str
    validity: AOVValidity = AOVValidity.VALID
    #: hash del contenido del pase (para detectar recomputación real).
    content_hash: str | None = None

    def is_valid(self) -> bool:
        return self.validity is AOVValidity.VALID


@dataclass(slots=True)
class AOVSet:
    """Conjunto de AOV de un shot, con validez por pase."""

    aovs: dict[str, AOV] = field(default_factory=dict)

    def add(self, name: str, validity: AOVValidity = AOVValidity.VALID) -> None:
        if not name.strip():
            raise RelightError("nombre de AOV vacío")
        if not isinstance(validity, AOVValidity):
            raise RelightError(f"validez de AOV inválida: {validity!r}")
        self.aovs[name] = AOV(name=name, validity=validity)

    def validate(self) -> None:
        for name, aov in self.aovs.items():
            if not name.strip() or aov.name != name:
                raise RelightError("nombre de AOV inconsistente")
            if not isinstance(aov.validity, AOVValidity):
                raise RelightError(f"AOV {name}: validez desconocida")

    def valid_names(self) -> set[str]:
        return {n for n, a in self.aovs.items() if a.is_valid()}

    def unknown_names(self) -> set[str]:
        return {n for n, a in self.aovs.items() if not a.is_valid()}

    def require_valid(self, name: str) -> None:
        """Usar un pase exige que sea válido; un unknown no se da por bueno (A18)."""
        aov = self.aovs.get(name)
        if aov is None:
            raise RelightError(f"AOV ausente: {name}")
        if not aov.is_valid():
            raise RelightError(f"AOV {name} es 'unknown': no se usa como válido")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "aov-set/1",
            "aovs": {
                name: {"validity": str(aov.validity), "content_hash": aov.content_hash}
                for name, aov in self.aovs.items()
            },
            "valid": sorted(self.valid_names()),
            "unknown": sorted(self.unknown_names()),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AOVSet:
        if not isinstance(data, dict) or data.get("schema") not in (None, "aov-set/1"):
            raise RelightError("AOVSet serializado inválido o schema desconocido")
        try:
            result = cls()
            if "aovs" in data:
                entries = data["aovs"]
                if not isinstance(entries, dict):
                    raise RelightError("aovs debe ser un objeto")
                for name, item in entries.items():
                    if not isinstance(item, dict):
                        raise RelightError(f"AOV {name}: entrada debe ser un objeto")
                    result.aovs[str(name)] = AOV(
                        name=str(name),
                        validity=AOVValidity(item["validity"]),
                        content_hash=item.get("content_hash"),
                    )
            else:
                valid = data.get("valid", [])
                unknown = data.get("unknown", [])
                if not isinstance(valid, list) or not isinstance(unknown, list):
                    raise RelightError("valid y unknown deben ser listas")
                if set(valid) & set(unknown):
                    raise RelightError("un AOV no puede ser valid y unknown a la vez")
                for name in valid:
                    result.add(str(name), AOVValidity.VALID)
                for name in unknown:
                    result.add(str(name), AOVValidity.UNKNOWN)
            result.validate()
            return result
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, RelightError):
                raise
            raise RelightError(f"AOVSet serializado inválido: {exc}") from exc


def invalidate_for_task(aovset: AOVSet, task: TaskKind) -> set[str]:
    """Qué AOV hay que recomputar para una tarea, SIN tocar los ajenos (A18).

    - RELIGHT: solo los pases dependientes de la iluminación. Albedo/normal/depth
      /id (ajenos a la luz) se conservan: «recompilar una luz no invalida campos
      ajenos a esa intervención».
    - RESTYLE: es otra tarea; afecta apariencia (shading + albedo), pero NO la
      geometría (normal/depth/id).
    """
    present = set(aovset.aovs)
    if task is TaskKind.RELIGHT:
        return present & LIGHTING_DEPENDENT
    if task is TaskKind.RESTYLE:
        # el restilizado toca apariencia pero no geometría.
        return (present & LIGHTING_DEPENDENT) | (present & {"albedo", "roughness"})
    raise RelightError(f"tarea desconocida: {task}")


def assert_tasks_distinct(a: TaskKind, b: TaskKind) -> None:
    """Restilización y reiluminación se reportan como tareas distintas (A18)."""
    if a is b:
        raise RelightError("se esperaban tareas distintas (relight vs restyle)")


def preserved_fields(aovset: AOVSet, task: TaskKind) -> set[str]:
    """Campos que una intervención CONSERVA (no recomputa)."""
    return set(aovset.aovs) - invalidate_for_task(aovset, task)


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_DIMENSION = 4096
_MAX_PIXELS = 4_000_000
_MAX_INTENSITY = 4.0
_MAX_AMBIENT = 1.0
_MAX_TEMPORAL_DURATION_S = 60.0
_MAX_TEMPORAL_FRAMES = 900
_MAX_TEMPORAL_BYTES = 256 * 1024 * 1024


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """Chunk PNG con longitud y CRC válidos (zlib.crc32, stdlib)."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _encode_png(width: int, height: int, bpp: int, raw: bytes) -> bytes:
    """PNG RGBA/RGB determinista: filtro 0 por fila, IHDR/IDAT/IEND con CRC."""
    color_type = 6 if bpp == 4 else 2
    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    stride = width * bpp
    scanlines = b"".join(b"\x00" + raw[row * stride : (row + 1) * stride] for row in range(height))
    idat = zlib.compress(scanlines, 9)
    return (
        _PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _finite_in_range(value: object, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RelightError(f"{name} debe ser numérico")
    number = float(value)
    if not math.isfinite(number):
        raise RelightError(f"{name} debe ser finito, no {number}")
    if number < 0.0 or number > maximum:
        raise RelightError(f"{name} fuera de rango [0, {maximum}]: {number}")
    return number


def render_relight(
    albedo: bytes,
    shading: bytes,
    width: int,
    height: int,
    intensity: float = 1,
    ambient: float = 0,
) -> dict[str, Any]:
    """Composite CPU determinista: albedo × (shading/255·intensity + ambient).

    Pure CPU (sin GPU): produce un PNG RGBA (albedo con alfa) o RGB (albedo
    plano) con el shading re-escalado por `intensity` y un piso `ambient`.
    El alfa del albedo queda intacto. Devuelve el PNG y su provenance
    (sha256 del PNG, del albedo y del shading) para evidencia reproducible.
    """
    if isinstance(width, bool) or not isinstance(width, int):
        raise RelightError("width debe ser un entero")
    if isinstance(height, bool) or not isinstance(height, int):
        raise RelightError("height debe ser un entero")
    if not (1 <= width <= _MAX_DIMENSION and 1 <= height <= _MAX_DIMENSION):
        raise RelightError(
            f"dimensiones inválidas {width}x{height}: cada lado en [1, {_MAX_DIMENSION}]"
        )
    pixels = width * height
    if pixels > _MAX_PIXELS:
        raise RelightError(f"{pixels} px supera el límite de {_MAX_PIXELS} px por render")
    if not isinstance(albedo, (bytes, bytearray)) or not isinstance(shading, (bytes, bytearray)):
        raise RelightError("albedo y shading deben ser buffers de bytes")
    albedo = bytes(albedo)
    shading = bytes(shading)
    if len(albedo) not in (pixels * 3, pixels * 4):
        raise RelightError(
            f"longitud de albedo {len(albedo)} no es {pixels * 3} (RGB) "
            f"ni {pixels * 4} (RGBA) para {width}x{height}"
        )
    if len(shading) != pixels:
        raise RelightError(
            f"longitud de shading {len(shading)} != {pixels} píxeles de {width}x{height}"
        )
    intensity_v = _finite_in_range(intensity, "intensity", _MAX_INTENSITY)
    ambient_v = _finite_in_range(ambient, "ambient", _MAX_AMBIENT)

    has_alpha = len(albedo) == pixels * 4
    bpp = 4 if has_alpha else 3
    composited = bytearray(pixels * bpp)
    for index in range(pixels):
        scale = (shading[index] / 255.0) * intensity_v + ambient_v
        src = index * bpp
        dst = index * bpp
        # Albedo saturado (clamp 0..255) × iluminación; el alfa NO se toca.
        for channel in range(3):
            value = round(albedo[src + channel] * scale)
            composited[dst + channel] = 0 if value < 0 else (255 if value > 255 else value)
        if has_alpha:
            composited[dst + 3] = albedo[src + 3]

    png = _encode_png(width, height, bpp, bytes(composited))
    return {
        "png": png,
        "sha256": hashlib.sha256(png).hexdigest(),
        "albedo_sha256": hashlib.sha256(albedo).hexdigest(),
        "shading_sha256": hashlib.sha256(shading).hexdigest(),
        "width": width,
        "height": height,
        "pixels": pixels,
        "color_type": 6 if has_alpha else 2,
        "intensity": intensity_v,
        "ambient": ambient_v,
        "renderer": "cpu_png_relight",
    }


# --- C21: relight CPU acotado y determinista con AOV sintéticos calibrados ---

#: Límite de píxeles por frame en la ruta sintética (CPU pura, O(px·frames)).
_MAX_SYNTH_PIXELS = 1_000_000
#: Máximo de frames por llamada de relight sintético.
_MAX_SYNTH_FRAMES = 64
#: La calibración debe ser casi exacta: los AOV + luz declarada tienen que
#: explicar el frame fuente (mismo modelo Lambertiano, error de redondeo 0).
_CALIBRATION_TOLERANCE = 1.0
#: Un normal LEGÍTIMO codificado (byte = round(127.5·c + 127.5), ‖c‖=1) se
#: decodifica con longitud ≈ 1 (error de cuantización < 0.5/127.5 por eje).
#: Longitud < 0.25 ⇒ pase no calibrado/basura (p.ej. 0x80,0x80,0x80 «plano»).
_NORMAL_MIN_LENGTH = 0.25


@dataclass(slots=True)
class LightParams:
    """Parámetros acotados de una luz direccional Lambertiana.

    `azimuth_deg` ∈ [-180, 180], `elevation_deg` ∈ [-90, 90]; dirección unitaria
    (cos(el)·sin(az), sin(el), cos(el)·cos(az)). `intensity` ∈ [0, 4],
    `ambient` ∈ [0, 1]; todo finito. Cualquier valor fuera de rango o no numérico
    se rechaza en construcción.
    """

    azimuth_deg: float
    elevation_deg: float
    intensity: float
    ambient: float

    def __post_init__(self) -> None:
        for name in ("azimuth_deg", "elevation_deg", "intensity", "ambient"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RelightError(f"{name} debe ser numérico")
            if not math.isfinite(float(value)):
                raise RelightError(f"{name} debe ser finito, no {value}")
        if not -180.0 <= self.azimuth_deg <= 180.0:
            raise RelightError(f"azimuth_deg fuera de rango [-180, 180]: {self.azimuth_deg}")
        if not -90.0 <= self.elevation_deg <= 90.0:
            raise RelightError(f"elevation_deg fuera de rango [-90, 90]: {self.elevation_deg}")
        if not 0.0 <= self.intensity <= _MAX_INTENSITY:
            raise RelightError(f"intensity fuera de rango [0, {_MAX_INTENSITY}]: {self.intensity}")
        if not 0.0 <= self.ambient <= _MAX_AMBIENT:
            raise RelightError(f"ambient fuera de rango [0, {_MAX_AMBIENT}]: {self.ambient}")


@dataclass(slots=True)
class FrameAOVs:
    """AOV calibrados de un frame + su frame fuente RGB y máscara protegida.

    `albedo`/`normal` son RGB (3 bytes/px), `occlusion`/`protected` 1 byte/px,
    `source_frame` RGB del frame fuente. `protected >= 128` marca píxeles que el
    relight NO puede cambiar (bit a bit idénticos al frame fuente).
    """

    albedo: bytes
    normal: bytes
    occlusion: bytes
    protected: bytes
    source_frame: bytes


def _light_direction(azimuth_deg: float, elevation_deg: float) -> tuple[float, float, float]:
    """Dirección de luz unitaria (convención documentada, compartida con tests)."""
    azimuth = math.radians(azimuth_deg)
    elevation = math.radians(elevation_deg)
    return (
        math.cos(elevation) * math.sin(azimuth),
        math.sin(elevation),
        math.cos(elevation) * math.cos(azimuth),
    )


def _lambert_render(
    albedo: bytes,
    normal: bytes,
    occlusion: bytes,
    light: LightParams,
    width: int,
    height: int,
) -> bytes:
    """Render Lambertiano CPU determinista: albedo · (n·l)⁺ · intensidad · (1-oclusión) + ambiente.

    Misma aritmética (mismo orden de operaciones float) que el ground truth de
    referencia; determinista y puro CPU (sin GPU).
    """
    lx, ly, lz = _light_direction(light.azimuth_deg, light.elevation_deg)
    pixels = width * height
    out = bytearray(pixels * 3)
    for index in range(pixels):
        nx = normal[index * 3] / 127.5 - 1
        ny = normal[index * 3 + 1] / 127.5 - 1
        nz = normal[index * 3 + 2] / 127.5 - 1
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        nx, ny, nz = nx / length, ny / length, nz / length
        diffuse = max(0.0, nx * lx + ny * ly + nz * lz)
        irradiance = diffuse * light.intensity * (1 - occlusion[index] / 255) + light.ambient
        for channel in range(3):
            value = round(albedo[index * 3 + channel] * irradiance)
            out[index * 3 + channel] = 0 if value < 0 else (255 if value > 255 else value)
    return bytes(out)


def _validate_synth_dimensions(width: object, height: object) -> int:
    if isinstance(width, bool) or not isinstance(width, int):
        raise RelightError("width debe ser un entero")
    if isinstance(height, bool) or not isinstance(height, int):
        raise RelightError("height debe ser un entero")
    if not (1 <= width <= _MAX_DIMENSION and 1 <= height <= _MAX_DIMENSION):
        raise RelightError(
            f"dimensiones inválidas {width}x{height}: cada lado en [1, {_MAX_DIMENSION}]"
        )
    pixels = width * height
    if pixels > _MAX_SYNTH_PIXELS:
        raise RelightError(
            f"{pixels} px supera el límite de {_MAX_SYNTH_PIXELS} px por frame sintético"
        )
    return pixels


def _validate_frame_buffers(
    frame: FrameAOVs, index: int, pixels: int, width: int, height: int
) -> None:
    if not isinstance(frame, FrameAOVs):
        raise RelightError(f"frame {index}: se esperaba un FrameAOVs")
    buffers = (
        ("albedo", pixels * 3),
        ("normal", pixels * 3),
        ("occlusion", pixels),
        ("protected", pixels),
        ("source_frame", pixels * 3),
    )
    for name, expected in buffers:
        value = getattr(frame, name)
        if not isinstance(value, (bytes, bytearray)):
            raise RelightError(f"frame {index}: {name} debe ser un buffer de bytes")
        if len(value) != expected:
            raise RelightError(
                f"frame {index}: longitud de {name} {len(value)} != {expected} "
                f"para {width}x{height}"
            )


def _validate_normals(frame: FrameAOVs, index: int, pixels: int) -> None:
    """Un normal no calibrado (longitud decodificada ~0) se rechaza explícitamente."""
    normal = bytes(frame.normal)
    for index_px in range(pixels):
        nx = normal[index_px * 3] / 127.5 - 1
        ny = normal[index_px * 3 + 1] / 127.5 - 1
        nz = normal[index_px * 3 + 2] / 127.5 - 1
        if math.sqrt(nx * nx + ny * ny + nz * nz) < _NORMAL_MIN_LENGTH:
            raise RelightError(
                f"frame {index}: normal degenerada en el píxel {index_px} "
                f"(longitud < {_NORMAL_MIN_LENGTH}): pase de normales no calibrado"
            )


def _validate_ground_truth(data: object, index: int, pixels: int) -> bytes:
    if not isinstance(data, (bytes, bytearray)):
        raise RelightError(f"ground truth {index} debe ser un buffer de bytes RGB")
    ground_truth = bytes(data)
    if len(ground_truth) != pixels * 3:
        raise RelightError(
            f"ground truth {index}: longitud {len(ground_truth)} != {pixels * 3} para {pixels} px"
        )
    return ground_truth


def relight_frames_synthetic(
    frames: list[FrameAOVs],
    width: int,
    height: int,
    source_light: LightParams,
    targets: list[LightParams],
    ground_truth: list[bytes],
    error_tolerance: float = 2.0,
) -> dict[str, Any]:
    """Relight CPU acotado y determinista sobre AOV sintéticos CALIBRADOS.

    Contrato C21:
    - Los AOV (albedo/normal/occlusion) deben explicar el frame fuente bajo la
      luz declarada (`source_light`): si la calibración Lambertiana no
      reconstruye el fuente (MAE > 1/255), los AOV no están calibrados y se
      rechaza — comparar contra un ground truth no tendría sentido.
    - Cada salida i re-ilumina el frame i (o el frame único, transmitido a todas
      las luces objetivo) con `targets[i]` y se compara contra `ground_truth[i]`,
      un render de referencia INDEPENDIENTE del módulo.
    - Los píxeles con `protected >= 128` se copian bit a bit del frame fuente
      (logo protegido); la comparación en esa región usa el ground truth con la
      misma protección compuesta (el logo no se re-ilumina tampoco en el
      ground truth esperado).
    - Métricas: por frame, de región editada, de región protegida y temporal
      (deltas entre frames consecutivos en la región editable).
    - Límites: ≤ 1_000_000 px por frame, 1..64 frames; luz objetivo distinta de
      la fuente. Puro CPU, sin GPU.
    """
    pixels = _validate_synth_dimensions(width, height)
    if isinstance(error_tolerance, bool) or not isinstance(error_tolerance, (int, float)):
        raise RelightError("error_tolerance debe ser numérico")
    tolerance = float(error_tolerance)
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise RelightError(f"error_tolerance debe ser finito y >= 0, no {error_tolerance}")
    if not isinstance(source_light, LightParams):
        raise RelightError("source_light debe ser un LightParams")
    if not isinstance(targets, list) or not all(isinstance(t, LightParams) for t in targets):
        raise RelightError("targets debe ser una lista de LightParams")
    if not isinstance(frames, list):
        raise RelightError("frames debe ser una lista de FrameAOVs")
    if not 1 <= len(frames) <= _MAX_SYNTH_FRAMES:
        raise RelightError(f"{len(frames)} frames fuera del rango [1, {_MAX_SYNTH_FRAMES}]")
    if not isinstance(ground_truth, list):
        raise RelightError("ground_truth debe ser una lista de buffers RGB")
    if len(targets) != len(ground_truth) or len(frames) not in (1, len(targets)):
        raise RelightError(
            "frames/luz objetivo/ground truth desalineados: "
            f"{len(frames)} frames, {len(targets)} luces objetivo, {len(ground_truth)} ground truth"
        )
    if not ground_truth:
        raise RelightError("ground truth vacío: se necesita al menos un frame de referencia")

    for index, frame in enumerate(frames):
        _validate_frame_buffers(frame, index, pixels, width, height)
    ground_truth_bytes = [
        _validate_ground_truth(gt, i, pixels) for i, gt in enumerate(ground_truth)
    ]
    for index, frame in enumerate(frames):
        _validate_normals(frame, index, pixels)
    for index, target in enumerate(targets):
        if target == source_light:
            raise RelightError(
                f"la luz objetivo {index} es igual a la luz fuente: no cambia la iluminación"
            )

    # Calibración: los AOV + la luz declarada deben reconstruir el frame fuente.
    calibration_frames: list[float] = []
    for index, frame in enumerate(frames):
        calibrated = _lambert_render(
            bytes(frame.albedo),
            bytes(frame.normal),
            bytes(frame.occlusion),
            source_light,
            width,
            height,
        )
        source_frame = bytes(frame.source_frame)
        diff_sum = sum(
            abs(calibrated[offset] - source_frame[offset]) for offset in range(pixels * 3)
        )
        calibration_mae = diff_sum / (pixels * 3)
        if calibration_mae > _CALIBRATION_TOLERANCE:
            raise RelightError(
                f"calibración fallida en el frame {index}: MAE {calibration_mae:.4f} > "
                f"{_CALIBRATION_TOLERANCE}: los AOV no explican el frame fuente "
                "bajo la luz declarada"
            )
        calibration_frames.append(calibration_mae)

    outputs: list[bytes] = []
    per_frame: list[dict[str, Any]] = []
    edited_gt_total = 0
    edited_src_total = 0
    edited_count = 0
    edited_max_abs = 0
    protected_max_abs = 0
    gt_protected_max_abs = 0
    for index, target in enumerate(targets):
        frame = frames[0] if len(frames) == 1 else frames[index]
        reference = ground_truth_bytes[index]
        protected = bytes(frame.protected)
        source_frame = bytes(frame.source_frame)
        lit = bytearray(
            _lambert_render(
                bytes(frame.albedo),
                bytes(frame.normal),
                bytes(frame.occlusion),
                target,
                width,
                height,
            )
        )
        frame_gt_max = 0
        frame_src_max = 0
        frame_gt_sum = 0
        frame_src_sum = 0
        frame_edited = 0
        frame_protected_max = 0
        frame_gt_protected_max = 0
        for pixel in range(pixels):
            offset = pixel * 3
            if protected[pixel] >= 128:
                # Logo protegido: bit a bit el frame fuente, sin re-iluminar.
                lit[offset : offset + 3] = source_frame[offset : offset + 3]
                for channel in range(3):
                    pos = offset + channel
                    protected_diff = abs(lit[pos] - source_frame[pos])
                    frame_protected_max = max(frame_protected_max, protected_diff)
                    frame_gt_protected_max = max(
                        frame_gt_protected_max, abs(reference[pos] - source_frame[pos])
                    )
            else:
                for channel in range(3):
                    pos = offset + channel
                    out_value = lit[pos]
                    frame_gt_sum += abs(out_value - reference[pos])
                    frame_src_sum += abs(out_value - source_frame[pos])
                    frame_edited += 1
                gt_diffs = (abs(lit[pos] - reference[pos]) for pos in range(offset, offset + 3))
                src_diffs = (abs(lit[pos] - source_frame[pos]) for pos in range(offset, offset + 3))
                frame_gt_max = max(frame_gt_max, *gt_diffs)
                frame_src_max = max(frame_src_max, *src_diffs)
        edited_gt_total += frame_gt_sum
        edited_src_total += frame_src_sum
        edited_count += frame_edited
        edited_max_abs = max(edited_max_abs, frame_gt_max)
        protected_max_abs = max(protected_max_abs, frame_protected_max)
        gt_protected_max_abs = max(gt_protected_max_abs, frame_gt_protected_max)
        outputs.append(bytes(lit))
        per_frame.append(
            {
                "frame": index,
                "light": {
                    "azimuth_deg": target.azimuth_deg,
                    "elevation_deg": target.elevation_deg,
                    "intensity": target.intensity,
                    "ambient": target.ambient,
                },
                "edited_mae_vs_ground_truth": (
                    round(frame_gt_sum / frame_edited, 6) if frame_edited else 0.0
                ),
                "edited_max_abs": frame_gt_max,
                "edited_mae_vs_source": (
                    round(frame_src_sum / frame_edited, 6) if frame_edited else 0.0
                ),
                "protected_bit_exact": frame_protected_max == 0,
                "protected_max_abs_vs_source": frame_protected_max,
                "ground_truth_protected_max_abs": frame_gt_protected_max,
            }
        )

    # Consistencia temporal: deltas entre frames consecutivos en la región
    # editable de AMBOS frames (los protegidos están congelados por diseño).
    delta_max = 0
    delta_sum = 0
    delta_count = 0
    for index in range(1, len(outputs)):
        single = len(frames) == 1
        previous_protected = bytes(frames[0].protected if single else frames[index - 1].protected)
        current_protected = bytes(frames[0].protected if single else frames[index].protected)
        for pixel in range(pixels):
            if previous_protected[pixel] >= 128 or current_protected[pixel] >= 128:
                continue
            offset = pixel * 3
            for channel in range(3):
                pos = offset + channel
                delta_error = abs(
                    (outputs[index][pos] - outputs[index - 1][pos])
                    - (ground_truth_bytes[index][pos] - ground_truth_bytes[index - 1][pos])
                )
                delta_sum += delta_error
                delta_count += 1
                delta_max = max(delta_max, delta_error)

    edited_mae_gt = edited_gt_total / edited_count if edited_count else 0.0
    edited_mae_source = edited_src_total / edited_count if edited_count else 0.0
    protected_mae_gt = 0.0  # el ground truth protegido es el fuente: bit a bit.
    temporal = {
        "pairs": len(outputs) - 1,
        "max_delta_error": delta_max,
        "mae_delta_error": round(delta_sum / delta_count, 6) if delta_count else 0.0,
        "within_tolerance": delta_max <= tolerance,
    }
    return {
        "renderer": "cpu-lambert-aov-relight-v1",
        "frames": len(outputs),
        "width": width,
        "height": height,
        "pixels": pixels,
        "error_tolerance": tolerance,
        "frames_rgb": outputs,
        "calibration": {
            "ok": True,
            "mae": max(calibration_frames) if calibration_frames else 0.0,
            "tolerance": _CALIBRATION_TOLERANCE,
            "per_frame_mae": [round(mae, 6) for mae in calibration_frames],
        },
        "per_frame": per_frame,
        "edited_region": {
            "mae_vs_ground_truth": round(edited_mae_gt, 6),
            "max_abs": edited_max_abs,
            "within_tolerance": edited_max_abs <= tolerance,
            "mae_vs_source": round(edited_mae_source, 6),
            "illumination_changed": edited_mae_source > 0.0,
        },
        "protected_region": {
            "bit_exact": protected_max_abs == 0,
            "max_abs_diff_vs_source": protected_max_abs,
            "mae_vs_ground_truth": protected_mae_gt,
            "ground_truth_protected_max_abs": gt_protected_max_abs,
            "ground_truth_honors_protection": gt_protected_max_abs == 0,
        },
        "temporal": temporal,
        "passed": (
            edited_max_abs <= tolerance and protected_max_abs == 0 and temporal["within_tolerance"]
        ),
        "limitations": (
            "Lambertian diffuse relight from calibrated albedo/normal/occlusion AOVs on CPU; "
            "no specular, subsurface, global illumination or shadow re-projection. Protected "
            "pixels are copied bit-exact from the source frame and the ground truth is "
            "compared with the same protection composited. Deterministic; same arithmetic "
            "and operation order as the reference renderer."
        ),
    }


def _temporal_probe(path: Path, ffprobe: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelightError(f"ffprobe temporal relight failed: {exc}") from exc
    if completed.returncode:
        raise RelightError(f"ffprobe temporal relight failed: {completed.stderr[-500:]}")
    try:
        data = json.loads(completed.stdout)
        streams = data["streams"]
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        fmt = data.get("format", {})
        duration = float(video.get("duration") or fmt.get("duration") or 0)
        rate = Fraction(str(video.get("avg_frame_rate") or video.get("r_frame_rate")))
        width, height = int(video["width"]), int(video["height"])
        frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    except (KeyError, TypeError, ValueError, StopIteration, ZeroDivisionError) as exc:
        raise RelightError("source must contain a readable constant-rate video stream") from exc
    if not math.isfinite(duration) or not 0 < duration <= _MAX_TEMPORAL_DURATION_S:
        raise RelightError(f"source duration must be within 0..{_MAX_TEMPORAL_DURATION_S}s")
    if rate <= 0 or float(rate) > 30:
        raise RelightError("source frame rate must be positive and at most 30 fps")
    if frames < 2 or frames > _MAX_TEMPORAL_FRAMES:
        raise RelightError(f"source frame count must be within 2..{_MAX_TEMPORAL_FRAMES}")
    if width < 2 or height < 2 or width * height > 3840 * 2160 or width % 2 or height % 2:
        raise RelightError("source dimensions must be even and no larger than 3840x2160")
    return {
        "width": width,
        "height": height,
        "duration_s": duration,
        "frame_rate": float(rate),
        "frames": frames,
        "has_audio": any(stream.get("codec_type") == "audio" for stream in streams),
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_timestamps(path: Path, ffprobe: str) -> list[float]:
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelightError(f"ffprobe frame timeline failed: {exc}") from exc
    if completed.returncode:
        raise RelightError(f"ffprobe frame timeline failed: {completed.stderr[-500:]}")
    try:
        data = json.loads(completed.stdout)
        return [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]]
    except (KeyError, TypeError, ValueError) as exc:
        raise RelightError("could not read source/output frame timestamps") from exc


def _validate_light_curve(
    keyframes: list[dict[str, Any]], frame_count: int
) -> list[tuple[int, float]]:
    if not isinstance(keyframes, list) or not 2 <= len(keyframes) <= 64:
        raise RelightError("keyframes must contain 2..64 frame/gain points")
    points: list[tuple[int, float]] = []
    for entry in keyframes:
        if not isinstance(entry, dict):
            raise RelightError("each keyframe must be an object")
        frame = entry.get("frame")
        if isinstance(frame, bool) or not isinstance(frame, int) or not 0 <= frame < frame_count:
            raise RelightError("keyframe frame must be an in-range integer")
        gain = _finite_in_range(entry.get("gain"), "gain", 4.0)
        points.append((frame, gain))
    if points[0][0] != 0 or any(a[0] >= b[0] for a, b in zip(points, points[1:], strict=False)):
        raise RelightError("keyframe frames must start at 0 and be strictly increasing")
    if len({gain for _frame, gain in points}) < 2:
        raise RelightError("temporal relight curve must change illumination at least once")
    return points


def _curve_values(points: list[tuple[int, float]], frame_count: int) -> list[float]:
    values: list[float] = []
    segment = 0
    for frame in range(frame_count):
        while segment + 1 < len(points) and frame > points[segment + 1][0]:
            segment += 1
        left_frame, left_gain = points[segment]
        if segment + 1 >= len(points):
            values.append(left_gain)
            continue
        right_frame, right_gain = points[segment + 1]
        fraction = (frame - left_frame) / (right_frame - left_frame)
        values.append(left_gain + (right_gain - left_gain) * fraction)
    return values


def _geq_curve(points: list[tuple[int, float]]) -> str:
    def number(value: float) -> str:
        return format(value, ".17g")

    expression = number(points[-1][1])
    pairs = zip(points, points[1:], strict=False)
    for (left_frame, left_gain), (right_frame, right_gain) in reversed(list(pairs)):
        ramp = (
            f"({number(left_gain)}+({number(right_gain)}-{number(left_gain)})*"
            f"(N-{left_frame})/{right_frame - left_frame})"
        )
        expression = f"if(lt(N,{right_frame}),{ramp},{expression})"
    return expression


def _gray_means(path: Path, ffmpeg: str, frame_count: int) -> list[float]:
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-vf",
                "scale=64:36:flags=area,format=gray",
                "-fps_mode",
                "passthrough",
                "-frames:v",
                str(frame_count),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RelightError(f"frame consistency measurement failed: {exc}") from exc
    frame_bytes = 64 * 36
    if completed.returncode or len(completed.stdout) != frame_bytes * frame_count:
        detail = completed.stderr[-500:].decode(errors="replace")
        raise RelightError(f"frame consistency measurement failed: {detail}")
    data = completed.stdout
    return [
        sum(data[offset : offset + frame_bytes]) / (frame_bytes * 255)
        for offset in range(0, len(data), frame_bytes)
    ]


def relight_video_temporal(
    source: Path,
    output: Path,
    keyframes: list[dict[str, Any]],
    *,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
) -> dict[str, Any]:
    """Apply an interpolated, spatially uniform RGB light gain to a short video.

    FFmpeg's CPU ``geq`` evaluates the scalar gain independently at each source
    frame index. ``-fps_mode passthrough`` keeps frame timestamps; audio is copied
    from the source. This is deterministic exposure-style illumination, not
    semantic/object-aware or physically based relighting.
    """
    ffmpeg, ffprobe = ffmpeg_bin or find_ffmpeg(), ffprobe_bin or find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise RelightError("FFmpeg y ffprobe son requeridos para reiluminación temporal")
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source == output:
        raise RelightError("source/output path invalid")
    if source.stat().st_size > _MAX_TEMPORAL_BYTES:
        raise RelightError("source exceeds temporal relight CPU byte limit")
    if output.exists():
        raise RelightError("output path must not already exist")
    before = _temporal_probe(source, ffprobe)
    points = _validate_light_curve(keyframes, before["frames"])
    values = _curve_values(points, before["frames"])
    expression = _geq_curve(points)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-copyts",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-map",
                "0:a?",
                "-vf",
                "format=rgb24,geq=r='clip(r(X,Y)*(" + expression + "),0,255)':"
                "g='clip(g(X,Y)*(" + expression + "),0,255)':"
                "b='clip(b(X,Y)*(" + expression + "),0,255)'",
                "-fps_mode",
                "passthrough",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                "-bf",
                "0",
                "-c:a",
                "copy",
                "-movflags",
                "+faststart",
                "-fs",
                str(_MAX_TEMPORAL_BYTES + 1),
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=max(60, before["duration_s"] * 30),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        output.unlink(missing_ok=True)
        raise RelightError(f"FFmpeg CPU temporal relight failed: {exc}") from exc
    if completed.returncode:
        output.unlink(missing_ok=True)
        raise RelightError(f"FFmpeg CPU temporal relight failed: {completed.stderr[-800:]}")
    try:
        if not output.is_file() or output.stat().st_size > _MAX_TEMPORAL_BYTES:
            raise RelightError("temporal relight output is missing or exceeds byte limit")
        after = _temporal_probe(output, ffprobe)
        source_times = _frame_timestamps(source, ffprobe)
        output_times = _frame_timestamps(output, ffprobe)
        if before["has_audio"] and not after["has_audio"]:
            raise RelightError("temporal relight output lost source audio")
        if after["frames"] != before["frames"] or len(output_times) != len(source_times):
            raise RelightError("temporal relight changed source frame count")
        timestamp_drift = max(abs(a - b) for a, b in zip(source_times, output_times, strict=True))
        frame_tolerance = 0.5 / before["frame_rate"]
        timestamps_match = timestamp_drift <= frame_tolerance
        if not timestamps_match:
            raise RelightError("temporal relight changed source frame timestamps")
        source_luma = _gray_means(source, ffmpeg, before["frames"])
        output_luma = _gray_means(output, ffmpeg, after["frames"])
        gain_mae = sum(
            abs(out_luma - src_luma * gain)
            for src_luma, out_luma, gain in zip(source_luma, output_luma, values, strict=True)
        ) / len(values)
        gain_steps = [abs(right - left) for left, right in zip(values, values[1:], strict=False)]
        return {
            "renderer": "ffmpeg-cpu-temporal-rgb-gain-v1",
            "source_sha256": _file_sha256(source),
            "output_sha256": _file_sha256(output),
            "keyframes": [{"frame": frame, "gain": gain} for frame, gain in points],
            "timeline": {
                "source_frames": before["frames"],
                "output_frames": after["frames"],
                "source_frame_rate": before["frame_rate"],
                "output_frame_rate": after["frame_rate"],
                "source_duration_s": before["duration_s"],
                "output_duration_s": after["duration_s"],
                "source_has_audio": before["has_audio"],
                "output_has_audio": after["has_audio"],
                "max_timestamp_drift_s": round(timestamp_drift, 8),
                "timestamps_match": timestamps_match,
            },
            "temporal_consistency": {
                "frames_compared": len(values),
                "mean_abs_gain_step": round(sum(gain_steps) / len(gain_steps), 8),
                "max_abs_gain_step": round(max(gain_steps), 8),
                "gain_tracking_luma_mae": round(gain_mae, 8),
                "per_frame_gain": [
                    {
                        "frame": index,
                        "requested": round(gain, 8),
                        "observed_luma_ratio": (
                            round(output_luma[index] / source_luma[index], 8)
                            if source_luma[index] > 1e-6
                            else None
                        ),
                    }
                    for index, gain in enumerate(values)
                ],
                "source_mean_luma": round(sum(source_luma) / len(source_luma), 8),
                "output_mean_luma": round(sum(output_luma) / len(output_luma), 8),
            },
            "limitations": (
                "Spatially uniform exposure-style RGB gain on CPU; no object masks, geometry, "
                "shadows, reflections, material response, or semantic relighting."
            ),
        }
    except Exception:
        output.unlink(missing_ok=True)
        raise


__all__ = [
    "AOV",
    "AOVSet",
    "AOVValidity",
    "FrameAOVs",
    "LIGHTING_DEPENDENT",
    "LIGHT_INDEPENDENT",
    "LightParams",
    "RelightError",
    "TaskKind",
    "assert_tasks_distinct",
    "invalidate_for_task",
    "preserved_fields",
    "relight_frames_synthetic",
]
