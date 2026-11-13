"""Arreglos musicales (C26): contrato metadata-only sobre stems existentes.

C26 pide arreglar la música del reel sin generarla: un grid temporal
(BPM + timebase), secciones `hit`/`loop` ancladas a stems YA almacenados en el
mix causal del proyecto (routers/audio.py) y variantes nombradas. Nada aquí
sintetiza audio ni reclama haberlo hecho: `Arrangement` describe qué stem del
proyecto suena en cada sección del reel (máx 20 s).

- `TempoGrid`: inmutable; su beat map es determinista (misma entrada → misma
  lista de tiempos, redondeada a 6 decimales).
- `ArrangementSection`: una sección del reel (`hit` o `loop`) ligada a un stem
  por nombre y opcionalmente a una variante nombrada.
- `Arrangement`: el reel completo con validación estricta (duración ≤ 20 s,
  ids únicos, secciones dentro del reel).
- `delete_stem_sections`: causalidad selectiva — borrar un stem elimina SOLO
  las secciones que lo referencian; el resto del arreglo se conserva
  (devuelve un arreglo nuevo; el original queda intacto).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

#: Duración máxima del reel que describe el arreglo (segundos).
MAX_REEL_DURATION_S = 20.0
#: Rango de BPM admitido (música de reel: nada razonable fuera de esto).
MIN_BPM = 20.0
MAX_BPM = 300.0
#: PPQ por defecto (pulses per quarter note), el timebase estándar de MIDI.
DEFAULT_TIMEBASE_PPQ = 480
#: Tipos de sección admitidos.
SECTION_KINDS = ("hit", "loop")
#: Decimales del beat map: salida determinista y serializable.
_BEAT_DECIMALS = 6


class MusicError(ValueError):
    """Contrato de arreglo musical inválido."""


@dataclass(frozen=True)
class TempoGrid:
    """Grid temporal inmutable: BPM y resolución del timebase (PPQ)."""

    bpm: float
    timebase_ppq: int = DEFAULT_TIMEBASE_PPQ

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if isinstance(self.bpm, bool) or not isinstance(self.bpm, (int, float)):
            raise MusicError("bpm debe ser numérico")
        if not math.isfinite(self.bpm) or not (MIN_BPM <= self.bpm <= MAX_BPM):
            raise MusicError(f"bpm fuera de rango [{MIN_BPM:.0f},{MAX_BPM:.0f}]")
        if isinstance(self.timebase_ppq, bool) or not isinstance(self.timebase_ppq, int):
            raise MusicError("timebase_ppq debe ser un entero")
        if self.timebase_ppq <= 0:
            raise MusicError("timebase_ppq debe ser > 0")

    def beat_interval_s(self) -> float:
        """Duración de una negra en segundos."""
        return 60.0 / float(self.bpm)

    def beat_map(self, duration_s: float) -> list[float]:
        """Tiempos de las negras dentro de [0, duration_s], determinista.

        Redondeo fijo a 6 decimales: la misma (bpm, ppq, duración) produce
        exactamente la misma lista, serializable y comparable.
        """
        self.validate()
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise MusicError("duration_s debe ser numérico")
        if not math.isfinite(duration_s) or duration_s < 0:
            raise MusicError("duration_s debe ser finito y >= 0")
        interval = self.beat_interval_s()
        beats: list[float] = []
        beat = 0.0
        index = 0
        while beat <= duration_s + 10.0**-_BEAT_DECIMALS:
            beats.append(round(beat, _BEAT_DECIMALS))
            index += 1
            beat = index * interval
        return beats

    def to_dict(self) -> dict[str, Any]:
        return {"bpm": float(self.bpm), "timebase_ppq": self.timebase_ppq}

    @classmethod
    def from_dict(cls, data: Any) -> TempoGrid:
        if not isinstance(data, dict):
            raise MusicError("tempo grid serializado inválido")
        try:
            return cls(
                bpm=float(data["bpm"]),
                timebase_ppq=int(data.get("timebase_ppq", DEFAULT_TIMEBASE_PPQ)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MusicError(f"tempo grid serializado inválido: {exc}") from exc


@dataclass(frozen=True)
class ArrangementSection:
    """Una sección del reel (`hit` o `loop`) ligada a un stem del proyecto.

    `stem` es el NOMBRE del stem ya almacenado en el mix causal del proyecto;
    la validación de pertenencia (mix de ESTE proyecto) la hace la ruta.
    """

    section_id: str
    kind: str  # "hit" | "loop"
    start_s: float
    duration_s: float
    stem: str
    variant: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not self.section_id.strip():
            raise MusicError("section_id vacío")
        if self.kind not in SECTION_KINDS:
            raise MusicError(
                f"sección {self.section_id}: kind {self.kind!r} no admitido "
                f"(solo {SECTION_KINDS})"
            )
        for field_name, value in (("stem", self.stem), ("variant", self.variant)):
            if value is not None and not str(value).strip():
                raise MusicError(f"sección {self.section_id}: {field_name} vacío")
        if isinstance(self.start_s, bool) or not math.isfinite(self.start_s) or self.start_s < 0:
            raise MusicError(f"sección {self.section_id}: start_s inválido")
        if (
            isinstance(self.duration_s, bool)
            or not math.isfinite(self.duration_s)
            or self.duration_s <= 0
        ):
            raise MusicError(f"sección {self.section_id}: duration_s inválido")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        data: dict[str, Any] = {
            "section_id": self.section_id,
            "kind": self.kind,
            "start_s": self.start_s,
            "duration_s": self.duration_s,
            "stem": self.stem,
        }
        if self.variant is not None:
            data["variant"] = self.variant
        return data

    @classmethod
    def from_dict(cls, data: Any) -> ArrangementSection:
        if not isinstance(data, dict):
            raise MusicError("sección serializada inválida")
        try:
            section = cls(
                section_id=str(data["section_id"]),
                kind=str(data["kind"]),
                start_s=float(data["start_s"]),
                duration_s=float(data["duration_s"]),
                stem=str(data["stem"]),
                variant=str(data["variant"]) if data.get("variant") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MusicError(f"sección serializada inválida: {exc}") from exc
        section.validate()
        return section


@dataclass(frozen=True)
class Arrangement:
    """El reel musical completo (máx 20 s) sobre stems del proyecto."""

    grid: TempoGrid
    sections: tuple[ArrangementSection, ...] = field(default_factory=tuple)
    duration_s: float = MAX_REEL_DURATION_S

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        self.grid.validate()
        if isinstance(self.duration_s, bool) or not isinstance(self.duration_s, (int, float)):
            raise MusicError("duration_s debe ser numérico")
        if not math.isfinite(self.duration_s) or not (0 < self.duration_s <= MAX_REEL_DURATION_S):
            raise MusicError(
                f"duration_s del reel inválido: (0,{MAX_REEL_DURATION_S:.0f}] s "
                f"requerido, hay {self.duration_s}"
            )
        seen: set[str] = set()
        for section in self.sections:
            section.validate()
            if section.section_id in seen:
                raise MusicError(f"section_id duplicado: {section.section_id}")
            seen.add(section.section_id)
            if section.start_s + section.duration_s > self.duration_s + 10.0**-_BEAT_DECIMALS:
                raise MusicError(
                    f"sección {section.section_id} se sale del reel [0,{self.duration_s}]"
                )

    def section_ids(self) -> list[str]:
        return [section.section_id for section in self.sections]

    def stems(self) -> list[str]:
        """Nombres de stem referenciados, sin duplicados y en orden dado."""
        return list(dict.fromkeys(section.stem for section in self.sections))

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "music-arrangement/1",
            "grid": self.grid.to_dict(),
            "duration_s": self.duration_s,
            "sections": [section.to_dict() for section in self.sections],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Arrangement:
        if not isinstance(data, dict) or data.get("schema") not in (None, "music-arrangement/1"):
            raise MusicError("arreglo serializado inválido o schema desconocido")
        try:
            arrangement = cls(
                grid=TempoGrid.from_dict(data.get("grid", {})),
                duration_s=float(data["duration_s"]),
                sections=tuple(
                    ArrangementSection.from_dict(section) for section in data.get("sections", [])
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, MusicError):
                raise
            raise MusicError(f"arreglo serializado inválido: {exc}") from exc
        arrangement.validate()
        return arrangement


def delete_stem_sections(arrangement: Arrangement, stem_name: str) -> tuple[Arrangement, list[str]]:
    """Borra un stem: elimina SOLO las secciones que lo referencian (causal).

    Devuelve `(nuevo_arreglo, ids_eliminados)`. El arreglo original no se
    muta; las secciones de otros stems se conservan tal cual.
    """
    removed = sorted(
        section.section_id for section in arrangement.sections if section.stem == stem_name
    )
    kept = tuple(section for section in arrangement.sections if section.stem != stem_name)
    updated = Arrangement(
        grid=arrangement.grid,
        sections=kept,
        duration_s=arrangement.duration_s,
    )
    return updated, removed


__all__ = [
    "DEFAULT_TIMEBASE_PPQ",
    "MAX_BPM",
    "MAX_REEL_DURATION_S",
    "MIN_BPM",
    "SECTION_KINDS",
    "Arrangement",
    "ArrangementSection",
    "MusicError",
    "TempoGrid",
    "delete_stem_sections",
]
