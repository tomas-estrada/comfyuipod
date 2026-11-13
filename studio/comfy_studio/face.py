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

import bisect
import math
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


@dataclass(slots=True)
class SpeakerMorphCurve:
    """Curva de morph por hablante, editable en el tiempo (W14 / A16).

    Liga morfos a una pista de audio por `audio_track_id`: claves de tiempo
    finitas, no negativas y ordenadas estrictamente; pesos finitos en [0,1].
    Símbolica: no es evidencia de calidad neural.
    """

    curve_id: str
    speaker_id: str
    audio_track_id: str
    #: claves de tiempo (s), finitas, >= 0, ordenadas estrictamente.
    times: list[float] = field(default_factory=list)
    #: weights[i][target] = peso 0..1 en times[i].
    weights: list[dict[str, float]] = field(default_factory=list)

    def validate(self) -> None:
        if not self.times:
            raise FaceError("curva sin claves de tiempo")
        # El orden de claves se valida ANTES de la longitud: si las claves están
        # desordenadas ese es el error dominante (diagnóstico por la causa raíz).
        previous: float | None = None
        for i, t in enumerate(self.times):
            if not math.isfinite(t):
                raise FaceError(f"clave de tiempo {i}: debe ser finito")
            if t < 0.0:
                raise FaceError(
                    f"clave de tiempo {i}: valor negativo ({t}); debe ser >= 0"
                )
            if previous is not None and t <= previous:
                raise FaceError(
                    f"claves de tiempo deben estar ordenadas de forma estricta: "
                    f"{previous} >= {t}"
                )
            previous = t
        if len(self.times) != len(self.weights):
            raise FaceError(
                "times y weights deben tener la misma longitud: "
                f"{len(self.times)} != {len(self.weights)}"
            )
        for i, w in enumerate(self.weights):
            for target, weight in w.items():
                if not math.isfinite(weight):
                    raise FaceError(
                        f"clave {i} morph {target}: peso debe ser finito"
                    )
                if not (0.0 <= weight <= 1.0):
                    raise FaceError(
                        f"clave {i} morph {target}: peso fuera de [0,1]"
                    )

    def sample(self, t: float) -> dict[str, float]:
        """Muestreo determinista: interpolación lineal y clamp a los extremos.

        Los objetivos son la unión de los morfos de todas las claves; una clave
        que no declara un objetivo aporta 0.0. Exige claves ordenadas (no se
        muestrea una curva corrupta sin validar).
        """
        previous: float | None = None
        for _i, key in enumerate(self.times):
            if previous is not None and key <= previous:
                raise FaceError(
                    f"claves de tiempo desordenadas: {previous} >= {key} "
                    f"(llama a validate() antes de sample())"
                )
            previous = key
        if not self.times:
            raise FaceError("curva sin claves de tiempo")
        times = self.times
        if t <= times[0]:
            i0 = i1 = 0
            frac = 0.0
        elif t >= times[-1]:
            i0 = i1 = len(times) - 1
            frac = 0.0
        else:
            i1 = bisect.bisect_right(times, t)
            i0 = i1 - 1
            frac = (t - times[i0]) / (times[i1] - times[i0])
        targets = sorted({name for w in self.weights for name in w})
        return {
            target: self.weights[i0].get(target, 0.0)
            + (self.weights[i1].get(target, 0.0) - self.weights[i0].get(target, 0.0))
            * frac
            for target in targets
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "curve_id": self.curve_id,
            "speaker_id": self.speaker_id,
            "audio_track_id": self.audio_track_id,
            "times": list(self.times),
            "weights": [dict(w) for w in self.weights],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SpeakerMorphCurve:
        curve = cls(
            curve_id=str(data["curve_id"]),
            speaker_id=str(data["speaker_id"]),
            audio_track_id=str(data["audio_track_id"]),
            times=[float(t) for t in data["times"]],
            weights=[
                {str(k): float(v) for k, v in w.items()} for w in data["weights"]
            ],
        )
        curve.validate()
        return curve


def assert_curve_matches_track(
    curve: SpeakerMorphCurve, perf: FacePerformance
) -> None:
    """La curva y la actuación deben ligarse a la MISMA pista de audio (A16)."""
    if curve.audio_track_id != perf.audio_track_id:
        raise FaceError(
            f"curva {curve.curve_id} ligada a otra pista de audio: "
            f"{curve.audio_track_id} != {perf.audio_track_id}"
        )


__all__ = [
    "AssetAvailability",
    "FaceError",
    "FaceIdentity",
    "FacePerformance",
    "SpeakerMorphCurve",
    "assert_curve_matches_track",
    "assert_gated_available",
    "assert_identity_persists",
    "edit_performance",
]
