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
