"""Avatar facial editable e identidad persistente (plan Ultra, W14 / A16).

W14 pide actuación facial/voz persistente, actores hablantes, memoria entre
planos y actuación temporal editable. El invariante (A16): un cambio de cámara/
luz NO regenera el audio; un modelo gated devuelve estado claro y no se
redistribuye como libre; un render Blender de morphs de fixture es evidencia CPU,
no prueba de calidad neural.

Este módulo implementa el CONTRATO:

- `FaceIdentity`: identidad facial persistente (id estable entre planos) con la
  licencia de sus pesos DECLARADA (ReActor/InsightFace son non-commercial) y su
  estado de disponibilidad (gated).
- `FacePerformance`: actuación facial por frame (morph targets) ligada a una
  pista de audio por `audio_track_id`.
- `edit_performance`: cambiar cámara/luz NO invalida el audio; cambiar el guion
  (audio) SÍ invalida la actuación labial — causalidad selectiva.
- `assert_gated_available`: un modelo gated ausente devuelve estado claro y no se
  finge libre.

Morph targets simbólicos; la síntesis facial real es engine GPU (A16 lo marca
como evidencia CPU, no calidad neural).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FaceError(ValueError):
    """Contrato facial inválido."""


class AssetAvailability(StrEnum):
    AVAILABLE = "available"
    GATED_MISSING = "gated_missing"   # requiere aceptar términos / no presente


@dataclass(slots=True)
class FaceIdentity:
    """Identidad facial persistente entre planos, con licencia declarada."""

    identity_id: str
    name: str
    #: licencia de los pesos faciales; ReActor/InsightFace son non-commercial.
    weights_license: str = "non-commercial"
    availability: AssetAvailability = AssetAvailability.AVAILABLE
    #: embedding de referencia (simbólico); su hash ancla la identidad.
    reference_hash: str | None = None

    def is_available(self) -> bool:
        return self.availability is AssetAvailability.AVAILABLE


@dataclass(slots=True)
class FacePerformance:
    """Actuación facial editable, ligada a una pista de audio por hablante."""

    performance_id: str
    identity_id: str
    audio_track_id: str
    #: morph targets por frame: frames[i][target] = peso 0..1.
    frames: list[dict[str, float]] = field(default_factory=list)
    #: parámetros de cámara/luz del plano (editables sin tocar el audio).
    camera_id: str | None = None
    light_id: str | None = None

    def validate(self) -> None:
        for i, f in enumerate(self.frames):
            for target, w in f.items():
                if not (0.0 <= w <= 1.0):
                    raise FaceError(f"frame {i} morph {target}: peso fuera de [0,1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "performance_id": self.performance_id,
            "identity_id": self.identity_id,
            "audio_track_id": self.audio_track_id,
            "frame_count": len(self.frames),
            "camera_id": self.camera_id,
            "light_id": self.light_id,
        }


#: qué invalida cada tipo de edición (causalidad selectiva, A16).
#: cambiar camara/luz NO toca el audio ni el lip-sync; cambiar el guion SI.
def edit_performance(perf: FacePerformance, *, change: str) -> set[str]:
    """Devuelve qué se recomputa ante una edición (A16).

    - `change="camera"` o `"light"`: solo se re-renderiza el plano; el AUDIO y
      la actuación labial (que depende del audio) se CONSERVAN.
    - `change="script"` (cambia el audio/texto): se recomputa la actuación labial
      (lip-sync) porque depende del audio; la identidad persiste.
    """
    if change in ("camera", "light"):
        return {"render"}  # audio y lip-sync intactos
    if change == "script":
        return {"audio", "lipsync", "render"}  # el audio cambió -> lip-sync sigue
    raise FaceError(f"tipo de edición desconocido: {change}")


def assert_identity_persists(a: FacePerformance, b: FacePerformance) -> None:
    """Dos planos del mismo personaje comparten identity_id (memoria entre planos)."""
    if a.identity_id != b.identity_id:
        raise FaceError(
            f"identidad no persistente entre planos: {a.identity_id} != {b.identity_id}"
        )


def assert_gated_available(identity: FaceIdentity) -> None:
    """Un modelo gated ausente devuelve estado claro, no se finge libre (A16)."""
    if not identity.is_available():
        raise FaceError(
            f"identidad {identity.identity_id}: modelo gated ausente "
            f"({identity.availability}); no se redistribuye como libre"
        )


__all__ = [
    "AssetAvailability",
    "FaceError",
    "FaceIdentity",
    "FacePerformance",
    "assert_gated_available",
    "assert_identity_persists",
    "edit_performance",
]
