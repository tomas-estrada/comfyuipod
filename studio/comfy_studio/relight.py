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

Opera sobre el grafo de pases, no sobre píxeles reales (el render es GPU/A29).
Verifica que la invalidación es selectiva y que los unknown son honestos.
"""

from __future__ import annotations

import hashlib
import math
import struct
import zlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


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
    scanlines = b"".join(
        b"\x00" + raw[row * stride : (row + 1) * stride] for row in range(height)
    )
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
        raise RelightError(
            f"{pixels} px supera el límite de {_MAX_PIXELS} px por render"
        )
    if not isinstance(albedo, (bytes, bytearray)) or not isinstance(
        shading, (bytes, bytearray)
    ):
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
