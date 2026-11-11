"""Foley, separación y sonido causal (plan Ultra, W18 / A20).

W18 pide foley dirigido por eventos/referencias con stems, mezcla con timebase
compartido (onsets/loudness) y el invariante causal: «borrar la causa elimina
solo los eventos vinculados». El invariante de build (A20): no descargar código
del engine desde nodos de ComfyUI en runtime.

Este módulo implementa el CONTRATO de sonido causal:

- `SoundEvent`: un evento de foley en un instante (onset), con loudness y una
  `caused_by` opcional (el id del evento visual/causa que lo origina).
- `Stem`: una pista separada (dialogo/foley/ambiente/musica) con sus eventos.
- `Mix`: la mezcla de stems con un timebase (fps) compartido.
- `delete_cause`: borra una causa visual y elimina SOLO los eventos de sonido
  vinculados a ella; los no vinculados se conservan — la misma causalidad
  selectiva de W15 aplicada al audio.

Opera sobre eventos/onsets simbólicos (no sintetiza audio real: eso es el engine
GPU/build, A20). Verifica la causalidad, el timebase compartido y la separación
de stems, que es lo que A20 pide a nivel de contrato.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


class AudioError(ValueError):
    """Contrato de audio inválido."""


@dataclass(slots=True)
class SoundEvent:
    """Un evento de sonido en un onset (segundos) con su causa opcional."""

    event_id: str
    onset_s: float
    loudness_lufs: float = -23.0  # EBU R128 de referencia
    #: id de la causa (evento visual/contacto) que origina este sonido, o None.
    caused_by: str | None = None

    def validate(self) -> None:
        if not self.event_id.strip():
            raise AudioError("event_id vacío")
        if not math.isfinite(self.onset_s) or self.onset_s < 0:
            raise AudioError(f"evento {self.event_id}: onset inválido")
        if not math.isfinite(self.loudness_lufs):
            raise AudioError(f"evento {self.event_id}: loudness no finito")
        if self.caused_by is not None and not self.caused_by.strip():
            raise AudioError(f"evento {self.event_id}: caused_by vacío")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "onset_s": self.onset_s,
            "loudness_lufs": self.loudness_lufs,
            "caused_by": self.caused_by,
        }


@dataclass(slots=True)
class Stem:
    """Una pista separada (no generada conjuntamente — invariante W19)."""

    name: str  # "dialogue" | "foley" | "ambience" | "music"
    events: list[SoundEvent] = field(default_factory=list)

    def onsets(self) -> list[float]:
        return sorted(e.onset_s for e in self.events)

    def to_dict(self) -> list[dict[str, Any]]:
        if not self.name.strip():
            raise AudioError("stem con nombre vacío")
        return [event.to_dict() for event in self.events]


@dataclass(slots=True)
class Mix:
    """Mezcla de stems con timebase compartido."""

    fps: int
    stems: list[Stem] = field(default_factory=list)
    #: 0 significa duración todavía sin declarar (no impone límite superior).
    duration_s: float = 0.0

    def validate(self) -> None:
        if self.fps <= 0 or not math.isfinite(self.duration_s) or self.duration_s < 0:
            raise AudioError("fps del timebase debe ser > 0")
        names = [s.name for s in self.stems]
        if len(names) != len(set(names)):
            raise AudioError("stems con nombre duplicado (deben estar separados)")
        event_ids: set[str] = set()
        for stem in self.stems:
            if not stem.name.strip():
                raise AudioError("stem con nombre vacío")
            for ev in stem.events:
                ev.validate()
                if ev.event_id in event_ids:
                    raise AudioError(f"event_id duplicado: {ev.event_id}")
                event_ids.add(ev.event_id)
                if ev.onset_s < 0 or (self.duration_s and ev.onset_s > self.duration_s):
                    raise AudioError(
                        f"evento {ev.event_id} fuera del timebase [0,{self.duration_s}]"
                    )

    def all_events(self) -> list[SoundEvent]:
        return [e for s in self.stems for e in s.events]

    def frame_of(self, onset_s: float) -> int:
        """Convierte un onset (s) a frame en el timebase compartido."""
        return int(round(onset_s * self.fps))

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "audio-mix/1",
            "fps": self.fps,
            "duration_s": self.duration_s,
            "stems": {s.name: [e.to_dict() for e in s.events] for s in self.stems},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Mix:
        if not isinstance(data, dict) or data.get("schema") not in (None, "audio-mix/1"):
            raise AudioError("mix serializado inválido o schema desconocido")
        stems_raw = data.get("stems", {})
        if not isinstance(stems_raw, dict) or any(
            not isinstance(events, list) for events in stems_raw.values()
        ):
            raise AudioError("stems serializados deben ser listas agrupadas por nombre")
        try:
            mix = cls(
                fps=int(data["fps"]),
                duration_s=float(data.get("duration_s", 0.0)),
                stems=[
                    Stem(
                        name=str(name),
                        events=[
                            SoundEvent(
                                event_id=str(event["event_id"]),
                                onset_s=float(event["onset_s"]),
                                loudness_lufs=float(event.get("loudness_lufs", -23.0)),
                                caused_by=(
                                    str(event["caused_by"])
                                    if event.get("caused_by") is not None
                                    else None
                                ),
                            )
                            for event in events
                        ],
                    )
                    for name, events in stems_raw.items()
                ],
            )
            mix.validate()
            return mix
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, AudioError):
                raise
            raise AudioError(f"mix serializado inválido: {exc}") from exc


def delete_cause(mix: Mix, cause_id: str) -> set[str]:
    """Borra una causa: elimina SOLO los eventos de sonido vinculados (A20).

    Los eventos sin `caused_by == cause_id` se conservan. Devuelve los event_id
    eliminados. No toca stems ajenos.
    """
    removed: set[str] = set()
    for stem in mix.stems:
        kept = []
        for ev in stem.events:
            if ev.caused_by == cause_id:
                removed.add(ev.event_id)
            else:
                kept.append(ev)
        stem.events = kept
    return removed


def assert_separate_stems(mix: Mix) -> None:
    """Las pistas deben estar SEPARADAS, no inferidas de una generación conjunta.

    Invariante W19: «no inferir stems por generación conjunta». Un mix con un
    único stem 'mixed' no acredita separación real.
    """
    if len(mix.stems) == 1 and mix.stems[0].name in ("mixed", "master"):
        raise AudioError("un único stem mezclado no acredita separación de pistas (A20/W19)")


__all__ = [
    "AudioError",
    "Mix",
    "SoundEvent",
    "Stem",
    "assert_separate_stems",
    "delete_cause",
]
