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
    "LIGHTING_DEPENDENT",
    "LIGHT_INDEPENDENT",
    "RelightError",
    "TaskKind",
    "assert_tasks_distinct",
    "invalidate_for_task",
    "preserved_fields",
]
