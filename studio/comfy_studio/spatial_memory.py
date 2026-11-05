"""Memoria espacial, multishot y reconstrucción 4D (plan Ultra, W17 / A19).

W17 pide memoria espacial entre planos y reconstrucción 4D con objetos/poses/
cámaras temporales editables. El invariante (A19): una secuencia A→B→A conserva
las anclas y registra el **drift**; ninguna imagen generada se promociona a
evidencia del atlas; se respetan límites de escena/duración/contexto.

Este módulo implementa el CONTRATO:

- `Anchor`: un ancla espacial persistente (id estable, posición) con procedencia
  observed/inferred (reutiliza la distinción de W09/W10).
- `SpatialMemory`: el registro de anclas entre planos; `visit` registra la
  posición observada de un ancla en un plano, y `drift` mide cuánto se movió un
  ancla entre dos visitas (A→B→A debe conservar el ancla con drift medido).
- `assert_not_atlas_evidence`: una posición de un ancla INFERIDA (generada) no se
  promociona a evidencia factual del atlas (A19).

Geometría vectorial simple; la reconstrucción 4D neural es GPU/A29. Verifica la
conservación de anclas, el drift y la honestidad de procedencia.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

Vec3 = tuple[float, float, float]


class Provenance(StrEnum):
    OBSERVED = "observed"
    INFERRED = "inferred"


class SpatialError(ValueError):
    """Contrato de memoria espacial inválido."""


def _dist(a: Vec3, b: Vec3) -> float:
    return math.sqrt(sum((a[i] - b[i]) ** 2 for i in range(3)))


@dataclass(slots=True)
class Anchor:
    """Un ancla espacial persistente entre planos."""

    anchor_id: str
    provenance: Provenance = Provenance.OBSERVED


@dataclass(slots=True)
class Visit:
    """La posición observada de un ancla en un plano concreto."""

    shot_id: str
    position: Vec3


@dataclass(slots=True)
class SpatialMemory:
    """Memoria de anclas entre planos; mide drift y conserva anclas (A19)."""

    anchors: dict[str, Anchor] = field(default_factory=dict)
    #: visitas por ancla, en orden temporal.
    visits: dict[str, list[Visit]] = field(default_factory=dict)
    #: límite de contexto por receta (nº de planos recordados).
    max_shots: int = 32

    def register(self, anchor: Anchor) -> None:
        self.anchors[anchor.anchor_id] = anchor
        self.visits.setdefault(anchor.anchor_id, [])

    def visit(self, anchor_id: str, shot_id: str, position: Vec3) -> None:
        if anchor_id not in self.anchors:
            raise SpatialError(f"ancla no registrada: {anchor_id}")
        visits = self.visits[anchor_id]
        if len({v.shot_id for v in visits} | {shot_id}) > self.max_shots:
            raise SpatialError(
                f"límite de contexto excedido ({self.max_shots} planos) para {anchor_id}"
            )
        visits.append(Visit(shot_id=shot_id, position=position))

    def drift(self, anchor_id: str) -> float:
        """Distancia entre la primera y la última visita del ancla (A→B→A)."""
        visits = self.visits.get(anchor_id, [])
        if len(visits) < 2:
            return 0.0
        return _dist(visits[0].position, visits[-1].position)

    def round_trip_drift(self, anchor_id: str) -> dict[str, Any]:
        """Para una secuencia A→B→A: ¿el ancla volvió a su sitio? Drift medido."""
        visits = self.visits.get(anchor_id, [])
        if len(visits) < 3:
            raise SpatialError(
                f"round-trip necesita >=3 visitas (A->B->A) para {anchor_id}"
            )
        return {
            "anchor_id": anchor_id,
            "shots": [v.shot_id for v in visits],
            "drift": round(self.drift(anchor_id), 6),
            "returned": self.drift(anchor_id) < 1e-6,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "anchors": {
                aid: {"provenance": str(a.provenance)} for aid, a in self.anchors.items()
            },
            "drift": {aid: round(self.drift(aid), 6) for aid in self.anchors},
        }


def assert_not_atlas_evidence(memory: SpatialMemory, anchor_id: str) -> None:
    """Un ancla INFERIDA no se promociona a evidencia factual del atlas (A19)."""
    anchor = memory.anchors.get(anchor_id)
    if anchor is None:
        raise SpatialError(f"ancla inexistente: {anchor_id}")
    if anchor.provenance is Provenance.INFERRED:
        raise SpatialError(
            f"ancla {anchor_id} INFERIDA (generada): no es evidencia del atlas"
        )


__all__ = [
    "Anchor",
    "Provenance",
    "SpatialError",
    "SpatialMemory",
    "Visit",
    "assert_not_atlas_evidence",
]
