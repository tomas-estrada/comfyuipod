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
import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class FaceError(ValueError):
    """Contrato facial inválido."""


class FaceLimitError(FaceError):
    """El artefacto o solicitud excede los límites finitos de procesamiento."""


MAX_SPEAKER_TURNS = 128
MAX_TURN_TEXT_CHARS = 4000
MAX_AUDIO_ASSET_BYTES = 50 * 1024 * 1024
MAX_CURVE_KEYS = 4096
MAX_MORPH_TARGETS = 64
MAX_CURVE_WEIGHTS = 50_000
MAX_ACTOR_FILE_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 256 * 1024 * 1024


_SPANISH_VISEMES = {
    "a": "open",
    "e": "wide",
    "i": "wide",
    "o": "round",
    "u": "round",
    "b": "closed",
    "p": "closed",
    "m": "closed",
    "f": "teeth",
    "v": "teeth",
}


def spanish_visemes(text: str, *, start: float, end: float) -> list[dict[str, Any]]:
    """Deterministic Spanish grapheme proxy; fixture mapping, not phoneme recognition."""
    if (
        not text.strip()
        or not math.isfinite(start)
        or not math.isfinite(end)
        or start < 0
        or end <= start
    ):
        raise FaceError("texto o intervalo de visemas inválido")
    units = [(c.lower(), _SPANISH_VISEMES.get(c.lower(), "closed")) for c in text if c.isalpha()]
    if not units:
        raise FaceError("texto sin letras para mapear")
    step = (end - start) / len(units)
    return [
        {"grapheme": c, "viseme": v, "start": start + i * step, "end": start + (i + 1) * step}
        for i, (c, v) in enumerate(units)
    ]


def compile_spanish_curve(
    *,
    curve_id: str,
    speaker_id: str,
    audio_track_id: str,
    text: str,
    start: float,
    end: float,
    rig_mapping: dict[str, str],
) -> SpeakerMorphCurve:
    """Compile fixture Spanish grapheme visemes to explicitly named rig shape keys."""
    visemes = spanish_visemes(text, start=start, end=end)
    needed = {item["viseme"] for item in visemes}
    if needed - rig_mapping.keys() or any(not value.strip() for value in rig_mapping.values()):
        raise FaceError("el rig_mapping debe declarar cada visema con su clave de morph")
    targets = sorted(set(rig_mapping.values()))
    times: list[float] = []
    weights: list[dict[str, float]] = []
    for item in visemes:
        times.append((item["start"] + item["end"]) / 2)
        weights.append(
            {name: 0.8 if name == rig_mapping[item["viseme"]] else 0.0 for name in targets}
        )
    curve = SpeakerMorphCurve(curve_id, speaker_id, audio_track_id, times, weights)
    curve.validate()
    return curve


def speaker_tracks(turns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Validate and retain distinct project audio stems, hashes, and timed turns."""
    if len(turns) > MAX_SPEAKER_TURNS:
        raise FaceError(f"máximo {MAX_SPEAKER_TURNS} turnos por solicitud")
    result: dict[str, list[dict[str, Any]]] = {}
    previous_end = -1.0
    for row in turns:
        try:
            speaker, track = str(row["speaker_id"]), str(row["audio_track_id"])
            start, end = float(row["start"]), float(row["end"])
            asset, digest, text = (
                str(row["audio_asset_id"]),
                str(row["audio_sha256"]),
                str(row["text"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FaceError("turno requiere hablante, pista, audio, hash, texto y tiempos") from exc
        if not speaker or not track or not asset or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise FaceError("proveniencia de pista de hablante inválida")
        if (
            len(speaker) > 64
            or len(track) > 64
            or len(asset) > 64
            or len(text) > MAX_TURN_TEXT_CHARS
        ):
            raise FaceError("identificador o texto de turno supera el límite permitido")
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < previous_end
            or end <= start
        ):
            raise FaceError("turnos desordenados o con intervalo inválido")
        previous_end = end
        result.setdefault(speaker, []).append(
            {
                "speaker_id": speaker,
                "audio_track_id": track,
                "audio_asset_id": asset,
                "audio_sha256": digest,
                "text": text,
                "start": start,
                "end": end,
            }
        )
    if not result:
        raise FaceError("se requiere al menos un turno")
    return result


def measure_speaker_fixture(
    *,
    predicted_speakers: list[str],
    expected_speakers: list[str],
    predicted_boundaries: list[float],
    expected_boundaries: list[float],
    viseme_boundaries: list[float],
    audio_boundaries: list[float],
) -> dict[str, Any]:
    """Report fixture-only identity, turn-boundary, and lip-drift measurements in seconds."""
    if len(predicted_boundaries) != len(expected_boundaries) or len(viseme_boundaries) != len(
        audio_boundaries
    ):
        raise FaceError("métricas requieren pares de límites predichos y esperados")
    if len(predicted_speakers) != len(expected_speakers):
        raise FaceError("métricas requieren igual número de etiquetas de hablante")
    values = [*predicted_boundaries, *expected_boundaries, *viseme_boundaries, *audio_boundaries]
    if any(not math.isfinite(value) for value in values):
        raise FaceError("métricas requieren valores finitos")

    def mae(a: list[float], b: list[float]) -> float:
        return sum(abs(x - y) for x, y in zip(a, b, strict=True)) / max(1, len(a))

    identity = sum(
        a == b for a, b in zip(predicted_speakers, expected_speakers, strict=True)
    ) / max(1, len(expected_speakers))
    return {
        "scope": "fixture_only",
        "speaker_identity_accuracy": identity,
        "turn_boundary_mae_seconds": mae(predicted_boundaries, expected_boundaries),
        "lip_drift_mae_seconds": mae(viseme_boundaries, audio_boundaries),
    }


def render_morph_glb(
    source: str, destination: str, curve: dict[str, Any], *, blender_bin: str | None = None
) -> dict[str, Any]:
    """Animate named existing shape keys in a project GLB and export a real GLB via Blender."""
    binary = blender_bin or shutil.which("blender")
    if not binary:
        raise FaceError("Blender no está instalado; render de morphs no ejecutado")
    morph = SpeakerMorphCurve.from_dict(curve)
    morph.validate()
    if len(morph.times) > MAX_CURVE_KEYS:
        raise FaceLimitError(f"máximo {MAX_CURVE_KEYS} claves por curva")
    targets = {name for row in morph.weights for name in row}
    if len(targets) > MAX_MORPH_TARGETS or len(morph.times) * len(targets) > MAX_CURVE_WEIGHTS:
        raise FaceLimitError("curva supera el límite de morph targets o pesos")
    from pathlib import Path

    source_path = Path(source)
    if not source_path.is_file() or source_path.stat().st_size > MAX_ACTOR_FILE_BYTES:
        raise FaceLimitError(f"actor ausente o supera {MAX_ACTOR_FILE_BYTES} bytes")
    payload = json.dumps({"source": source, "destination": destination, "curve": morph.to_dict()})
    script = r"""import bpy, json, sys
p=json.loads(sys.argv[-1]); bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=p["source"])
keys=[]
for obj in bpy.data.objects:
 if obj.type == 'MESH' and obj.data.shape_keys:
  for name in {n for row in p['curve']['weights'] for n in row}:
   if name in obj.data.shape_keys.key_blocks:
    keys.append((obj,obj.data.shape_keys.key_blocks[name]))
requested={n for row in p['curve']['weights'] for n in row}
found={k.name for _,k in keys}
if requested-found: raise RuntimeError('rig missing shape keys: '+','.join(sorted(requested-found)))
scene=bpy.context.scene; scene.render.fps=30
for i,t in enumerate(p['curve']['times']):
 frame=round(t*scene.render.fps)+1
 for obj in bpy.data.objects:
  if obj.type=='MESH' and obj.data.shape_keys:
   for name in requested:
    kb=obj.data.shape_keys.key_blocks.get(name)
    if kb:
     kb.value=float(p['curve']['weights'][i].get(name,0.0))
     kb.keyframe_insert(data_path='value',frame=frame,
                        group='Speaker '+p['curve']['speaker_id'])
scene.frame_start=1; scene.frame_end=max(1,round(p['curve']['times'][-1]*scene.render.fps)+1)
bpy.ops.export_scene.gltf(filepath=p['destination'],export_format='GLB',export_animations=True,export_morph=True,export_extras=True)
"""
    with tempfile.NamedTemporaryFile("w", suffix=".py", encoding="utf8") as f:
        f.write(script)
        f.flush()
        proc = subprocess.run(
            [binary, "--background", "--python", f.name, "--", payload],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    if proc.returncode or not __import__("pathlib").Path(destination).is_file():
        raise FaceError("Blender no pudo exportar morphs: " + (proc.stderr or proc.stdout)[-1200:])
    if __import__("pathlib").Path(destination).stat().st_size > MAX_OUTPUT_FILE_BYTES:
        raise FaceLimitError(f"GLB de salida supera {MAX_OUTPUT_FILE_BYTES} bytes")
    return {
        "engine": "Blender",
        "cpu_artifact": True,
        "neural_generation": "not_run",
        "source_sha256": hashlib.sha256(
            __import__("pathlib").Path(source).read_bytes()
        ).hexdigest(),
        "output_sha256": hashlib.sha256(
            __import__("pathlib").Path(destination).read_bytes()
        ).hexdigest(),
    }


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
                raise FaceError(f"clave de tiempo {i}: valor negativo ({t}); debe ser >= 0")
            if previous is not None and t <= previous:
                raise FaceError(
                    f"claves de tiempo deben estar ordenadas de forma estricta: {previous} >= {t}"
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
                    raise FaceError(f"clave {i} morph {target}: peso debe ser finito")
                if not (0.0 <= weight <= 1.0):
                    raise FaceError(f"clave {i} morph {target}: peso fuera de [0,1]")

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
            + (self.weights[i1].get(target, 0.0) - self.weights[i0].get(target, 0.0)) * frac
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
            weights=[{str(k): float(v) for k, v in w.items()} for w in data["weights"]],
        )
        curve.validate()
        return curve


def assert_curve_matches_track(curve: SpeakerMorphCurve, perf: FacePerformance) -> None:
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
