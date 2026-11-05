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


@dataclass(slots=True)
class Mix:
    """Mezcla de stems con timebase compartido."""

    fps: int
    stems: list[Stem] = field(default_factory=list)
    duration_s: float = 0.0

    def validate(self) -> None:
        if self.fps <= 0:
            raise AudioError("fps del timebase debe ser > 0")
        names = [s.name for s in self.stems]
        if len(names) != len(set(names)):
            raise AudioError("stems con nombre duplicado (deben estar separados)")
        for stem in self.stems:
            for ev in stem.events:
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
        return {
            "fps": self.fps,
            "duration_s": self.duration_s,
            "stems": {
                s.name: [e.to_dict() for e in s.events] for s in self.stems
            },
        }


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
        raise AudioError(
            "un único stem mezclado no acredita separación de pistas (A20/W19)"
        )


__all__ = [
    "AudioError",
    "Mix",
    "SoundEvent",
    "Stem",
    "assert_separate_stems",
    "delete_cause",
]
