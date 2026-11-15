"""Plan de sonido por escena acotado en CPU (C25 / A20).

C25 pide: «Video con pasos/frenado/ambiente: evento a tiempo y stems
editables; medir onset, loudness y relación con acciones».

Este módulo implementa el mapeo DECLARADO de escena a sonido:

- `VideoAction`: una acción del video fuente con su intervalo de tiempo
  (`start_s`..`end_s`) y su kind acotado (`steps` | `braking` | `ambience`).
- `SceneSoundCue`: el mapeo de una acción a una muestra de audio PROPIA del
  proyecto, un stem editable, un onset dentro de la ventana de la acción
  (± tolerancia) y licencia/attribution declaradas.
- `SceneSoundPlan`: el plan tipado completo con el timebase del video fuente
  (fps + duración) y los límites acotados (acciones/cues/tolerancia).

El render reutiliza la ruta real de C25 en `audio.py` (FFmpeg por argv,
PCM s16 estéreo 48 kHz, stems separados) y añade:

- verificación del timebase del video fuente contra ffprobe (fps y duración);
- alineación de cada onset al grid de frames del timebase compartido;
- medición del onset REAL de cada evento renderizado (decodificación del
  PCM del stem aislado) comparada contra el onset previsto;
- medición de loudness integrado y true peak (loudnorm EBU R128, ya medida
  por `render_stems`) conservada en la procedencia;
- procedencia persistida con hashes SHA-256 del video fuente, las acciones
  declaradas, las muestras y los stems de salida.

Sin generación ni separación por modelo: solo CPU, muestras del usuario y
FFmpeg acotado. Nada de claims neuronales ni de GPU.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import tempfile
from array import array
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio import (
    MAX_MIX_DURATION_S,
    MAX_STEM_EVENTS,
    RENDER_TIMEOUT_S,
    TARGET_SAMPLE_RATE,
    Mix,
    RenderedStem,
    SampleValidationError,
    SoundEvent,
    Stem,
    _resolve_bins,
    _run_argv,
    assert_separate_stems,
    frame_aligned_delay_ms,
    mix_digest,
    render_stems,
)
from .media_bins import find_ffprobe


class SceneSoundError(ValueError):
    """Plan de sonido por escena inválido o inconsistente con la fuente."""


SCENE_SOUND_PLAN_SCHEMA = "scene-sound-plan/1"
SCENE_SOUND_PROVENANCE_SCHEMA = "scene-sound-provenance/1"

#: Kinds de acción acotados (C25): pasos, frenado y ambiente.
ACTION_KINDS = frozenset({"steps", "braking", "ambience"})

#: Límite de acciones declaradas por plan: procesamiento acotado y determinista.
MAX_ACTIONS = 16
#: Límite total de cues (eventos) por plan.
MAX_CUES = MAX_STEM_EVENTS
#: Tolerancia máxima permitida para «onset dentro de la ventana de la acción».
MAX_ONSET_TOLERANCE_S = 0.5
#: Tolerancia por defecto.
DEFAULT_ONSET_TOLERANCE_S = 0.25
#: Desviación máxima aceptada entre el timebase declarado y el video fuente.
SOURCE_TIMEBASE_TOLERANCE_S = 0.5
#: Tolerancia fps entre lo declarado y lo medido con ffprobe.
SOURCE_FPS_TOLERANCE = 0.5
#: Tolerancia para «onset medido ≈ onset previsto» en el stem renderizado.
ONSET_MEASURE_TOLERANCE_S = 0.01
#: Nombre fijo del archivo de procedencia.
PROVENANCE_FILENAME = "scene_sound_provenance.json"


def _finite(value: Any, what: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SceneSoundError(f"{what} debe ser numérico") from exc
    if not math.isfinite(number):
        raise SceneSoundError(f"{what} debe ser finito")
    return number


@dataclass(slots=True)
class VideoAction:
    """Una acción del video fuente con su intervalo temporal."""

    action_id: str
    kind: str  # "steps" | "braking" | "ambience"
    start_s: float
    end_s: float

    def validate(self) -> None:
        if not self.action_id.strip():
            raise SceneSoundError("action_id vacío")
        if self.kind not in ACTION_KINDS:
            raise SceneSoundError(
                f"acción {self.action_id}: kind {self.kind!r} no acotado; "
                f"usar uno de {sorted(ACTION_KINDS)}"
            )
        start = _finite(self.start_s, f"acción {self.action_id}: start_s")
        end = _finite(self.end_s, f"acción {self.action_id}: end_s")
        if start < 0 or end <= start:
            raise SceneSoundError(
                f"acción {self.action_id}: ventana inválida ({start}..{end})"
            )

    def contains(self, onset_s: float, tolerance_s: float) -> bool:
        """¿El onset cae en la ventana de la acción ± tolerancia?"""
        return (self.start_s - tolerance_s) <= onset_s <= (self.end_s + tolerance_s)

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "start_s": self.start_s,
            "end_s": self.end_s,
        }

    @classmethod
    def from_dict(cls, data: Any) -> VideoAction:
        if not isinstance(data, dict):
            raise SceneSoundError("acción serializada inválida")
        return cls(
            action_id=str(data["action_id"]),
            kind=str(data["kind"]),
            start_s=_finite(data["start_s"], "start_s"),
            end_s=_finite(data["end_s"], "end_s"),
        )


@dataclass(slots=True)
class SceneSoundCue:
    """Mapeo de una acción a una muestra propia del proyecto y un stem editable."""

    cue_id: str
    action_id: str
    stem: str
    sample_asset_id: str
    onset_s: float
    loudness_lufs: float = -23.0
    sample_license: str | None = None
    sample_attribution: str | None = None

    def validate(self) -> None:
        for name, value in (
            ("cue_id", self.cue_id),
            ("action_id", self.action_id),
            ("stem", self.stem),
            ("sample_asset_id", self.sample_asset_id),
        ):
            if not str(value).strip():
                raise SceneSoundError(f"cue {name} vacío")
        _finite(self.onset_s, f"cue {self.cue_id}: onset_s")
        _finite(self.loudness_lufs, f"cue {self.cue_id}: loudness_lufs")
        for field_name, value in (
            ("sample_license", self.sample_license),
            ("sample_attribution", self.sample_attribution),
        ):
            if value is None or not str(value).strip():
                raise SceneSoundError(
                    f"cue {self.cue_id}: cada muestra requiere {field_name} declarado"
                )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "cue_id": self.cue_id,
            "action_id": self.action_id,
            "stem": self.stem,
            "sample_asset_id": self.sample_asset_id,
            "onset_s": self.onset_s,
            "loudness_lufs": self.loudness_lufs,
            "sample_license": self.sample_license,
            "sample_attribution": self.sample_attribution,
        }

    @classmethod
    def from_dict(cls, data: Any) -> SceneSoundCue:
        if not isinstance(data, dict):
            raise SceneSoundError("cue serializado inválido")
        return cls(
            cue_id=str(data["cue_id"]),
            action_id=str(data["action_id"]),
            stem=str(data["stem"]),
            sample_asset_id=str(data["sample_asset_id"]),
            onset_s=_finite(data["onset_s"], "onset_s"),
            loudness_lufs=_finite(data.get("loudness_lufs", -23.0), "loudness_lufs"),
            sample_license=(
                str(data["sample_license"]) if data.get("sample_license") is not None else None
            ),
            sample_attribution=(
                str(data["sample_attribution"])
                if data.get("sample_attribution") is not None
                else None
            ),
        )


def frame_of_cue(cue: SceneSoundCue, fps: int) -> int:
    """Frame del timebase compartido al que se alinea el onset del cue."""
    _delay_ms, frame = frame_aligned_delay_ms(cue.onset_s, fps)
    return frame


@dataclass(slots=True)
class SceneSoundPlan:
    """Plan tipado de sonido por escena (C25) sobre el timebase del video fuente."""

    source_video_asset_id: str
    fps: int
    duration_s: float
    actions: list[VideoAction] = field(default_factory=list)
    cues: list[SceneSoundCue] = field(default_factory=list)
    onset_tolerance_s: float = DEFAULT_ONSET_TOLERANCE_S

    def validate(self) -> None:
        if not str(self.source_video_asset_id).strip():
            raise SceneSoundError("source_video_asset_id vacío")
        if isinstance(self.fps, bool) or not isinstance(self.fps, int) or self.fps <= 0:
            raise SceneSoundError("fps del timebase del video fuente debe ser entero > 0")
        duration = _finite(self.duration_s, "duration_s")
        if duration <= 0 or duration > MAX_MIX_DURATION_S:
            raise SceneSoundError(
                f"duration_s {duration} fuera de rango (0, {MAX_MIX_DURATION_S}]"
            )
        tolerance = _finite(self.onset_tolerance_s, "tolerancia de onset")
        if tolerance <= 0 or tolerance > MAX_ONSET_TOLERANCE_S:
            raise SceneSoundError(
                f"tolerancia de onset {tolerance} fuera de rango "
                f"(0, {MAX_ONSET_TOLERANCE_S}]"
            )
        if not self.actions:
            raise SceneSoundError("el plan necesita al menos una acción del video fuente")
        if len(self.actions) > MAX_ACTIONS:
            raise SceneSoundError(f"demasiadas acciones ({len(self.actions)} > {MAX_ACTIONS})")
        if not self.cues:
            raise SceneSoundError("el plan necesita al menos un cue (mapeo acción→muestra)")
        if len(self.cues) > MAX_CUES:
            raise SceneSoundError(f"demasiados cues ({len(self.cues)} > {MAX_CUES})")
        action_ids: set[str] = set()
        for action in self.actions:
            action.validate()
            if action.end_s > duration:
                raise SceneSoundError(
                    f"acción {action.action_id}: end_s {action.end_s} excede duration_s"
                )
            if action.action_id in action_ids:
                raise SceneSoundError(f"action_id duplicado: {action.action_id}")
            action_ids.add(action.action_id)
        by_id = {action.action_id: action for action in self.actions}
        cue_ids: set[str] = set()
        for cue in self.cues:
            cue.validate()
            if cue.cue_id in cue_ids:
                raise SceneSoundError(f"cue_id duplicado: {cue.cue_id}")
            cue_ids.add(cue.cue_id)
            action = by_id.get(cue.action_id)
            if action is None:
                raise SceneSoundError(f"cue {cue.cue_id}: acción {cue.action_id} desconocida")
            onset = _finite(cue.onset_s, f"cue {cue.cue_id}: onset_s")
            if onset < 0 or onset > duration:
                raise SceneSoundError(
                    f"cue {cue.cue_id}: onset {onset} fuera del timebase [0,{duration}]"
                )
            if not action.contains(onset, tolerance):
                raise SceneSoundError(
                    f"cue {cue.cue_id}: onset {onset}s fuera de la ventana de la acción "
                    f"{cue.action_id} [{action.start_s},{action.end_s}] ± {tolerance}s"
                )

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": SCENE_SOUND_PLAN_SCHEMA,
            "source_video_asset_id": self.source_video_asset_id,
            "fps": self.fps,
            "duration_s": self.duration_s,
            "onset_tolerance_s": self.onset_tolerance_s,
            "actions": [action.to_dict() for action in self.actions],
            "cues": [cue.to_dict() for cue in self.cues],
        }

    @classmethod
    def from_dict(cls, data: Any) -> SceneSoundPlan:
        if not isinstance(data, dict) or data.get("schema") not in (None, SCENE_SOUND_PLAN_SCHEMA):
            raise SceneSoundError("plan de sonido por escena inválido o schema desconocido")
        if not isinstance(data.get("actions"), list) or not isinstance(data.get("cues"), list):
            raise SceneSoundError("plan serializado requiere actions y cues como listas")
        try:
            plan = cls(
                source_video_asset_id=str(data["source_video_asset_id"]),
                fps=int(data["fps"]),
                duration_s=_finite(data["duration_s"], "duration_s"),
                actions=[VideoAction.from_dict(item) for item in data["actions"]],
                cues=[SceneSoundCue.from_dict(item) for item in data["cues"]],
                onset_tolerance_s=_finite(
                    data.get("onset_tolerance_s", DEFAULT_ONSET_TOLERANCE_S),
                    "tolerancia de onset",
                ),
            )
            plan.validate()
            return plan
        except SceneSoundError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise SceneSoundError(f"plan serializado inválido: {exc}") from exc


def build_mix(plan: SceneSoundPlan) -> tuple[Mix, dict[str, tuple[float, int]]]:
    """Construye el mix editable del plan y la colocación efectiva de cada cue.

    Devuelve `(mix, placement)` donde `placement[cue_id]` es
    `(onset_efectivo_s, frame)`: el onset alineado al grid de frames del
    timebase del video fuente. Los stems quedan separados por nombre y cada
    evento mantiene `caused_by = action_id` (relación con la acción).
    """
    plan.validate()
    stems: dict[str, list[SoundEvent]] = {}
    placement: dict[str, tuple[float, int]] = {}
    for cue in plan.cues:
        delay_ms, frame = frame_aligned_delay_ms(cue.onset_s, plan.fps)
        effective = delay_ms / 1000.0
        stems.setdefault(cue.stem, []).append(
            SoundEvent(
                event_id=cue.cue_id,
                onset_s=effective,
                loudness_lufs=cue.loudness_lufs,
                caused_by=cue.action_id,
                sample_asset_id=cue.sample_asset_id,
                sample_license=cue.sample_license,
                sample_attribution=cue.sample_attribution,
            )
        )
        placement[cue.cue_id] = (effective, frame)
    mix = Mix(
        fps=plan.fps,
        duration_s=plan.duration_s,
        stems=[Stem(name=name, events=stems[name]) for name in sorted(stems)],
    )
    mix.validate()
    assert_separate_stems(mix)
    return mix, placement


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sha256(data: Any) -> str:
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def plan_digest(plan: SceneSoundPlan) -> str:
    """Digest determinista del plan serializado (sensible a cualquier edición)."""
    return _canonical_sha256(plan.to_dict())


def probe_source_timebase(
    source_video: str | Path,
    *,
    ffprobe_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> dict[str, Any]:
    """Timebase real del video fuente medido con ffprobe (fps/duración/frames)."""
    _ff, fp = _resolve_bins(None, ffprobe_bin or find_ffprobe())
    source = Path(source_video)
    if not source.is_file():
        raise SceneSoundError(f"video fuente {source} no existe en disco")
    proc = _run_argv(
        [
            fp,
            "-v",
            "error",
            "-show_entries",
            "stream=r_frame_rate,avg_frame_rate,nb_frames:format=duration",
            "-of",
            "json",
            str(source),
        ],
        timeout_s,
        "ffprobe (timebase del video fuente)",
    )
    if proc.returncode != 0:
        raise SceneSoundError(
            f"video fuente {source} no es legible (ffprobe): {proc.stderr.strip()[-300:]}"
        )
    try:
        data = json.loads(proc.stdout)
        stream = data["streams"][0]
        rate = str(stream.get("r_frame_rate") or stream["avg_frame_rate"])
        numerator, _, denominator = rate.partition("/")
        fps = float(numerator) / (float(denominator) if denominator else 1.0)
        duration_s = float(data["format"]["duration"])
    except (
        KeyError,
        IndexError,
        TypeError,
        ValueError,
        ZeroDivisionError,
        json.JSONDecodeError,
    ) as exc:
        raise SceneSoundError(f"video fuente {source} sin timebase medible: {exc}") from exc
    if not math.isfinite(fps) or fps <= 0 or not math.isfinite(duration_s) or duration_s <= 0:
        raise SceneSoundError(f"video fuente {source} con timebase inválido")
    frame_count = int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None
    return {
        "fps": fps,
        "r_frame_rate": rate,
        "duration_s": duration_s,
        "frame_count": frame_count,
    }


def verify_source_timebase(
    plan: SceneSoundPlan,
    source_video: str | Path,
    *,
    ffprobe_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> dict[str, Any]:
    """El timebase declarado del plan debe coincidir con el video fuente real."""
    probe = probe_source_timebase(source_video, ffprobe_bin=ffprobe_bin, timeout_s=timeout_s)
    if abs(probe["fps"] - plan.fps) > SOURCE_FPS_TOLERANCE:
        raise SceneSoundError(
            f"timebase del video fuente incompatible: fps medido {probe['fps']:.3f} "
            f"vs declarado {plan.fps}"
        )
    if abs(probe["duration_s"] - plan.duration_s) > SOURCE_TIMEBASE_TOLERANCE_S:
        raise SceneSoundError(
            f"duration_s del video fuente incompatible: medido {probe['duration_s']:.3f}s "
            f"vs declarado {plan.duration_s:.3f}s"
        )
    return probe


def measure_first_onset_s(
    path: str | Path,
    *,
    ffmpeg_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
    threshold: int = 128,
    sustain_samples: int = 24,
) -> float | None:
    """Mide el onset REAL del primer sonido en un WAV renderizado.

    Decodifica el PCM s16le (mono 48 kHz) y devuelve el instante (s) en que la
    señal supera el umbral de forma sostenida. `None` si el archivo es
    silencioso de principio a fin.
    """
    ff, _ = _resolve_bins(ffmpeg_bin, None)
    source = Path(path)
    if not source.is_file():
        raise SceneSoundError(f"stem {source} no existe para medir onset")
    proc = subprocess.run(
        [
            ff,
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(source),
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            str(TARGET_SAMPLE_RATE),
            "-",
        ],
        capture_output=True,
        timeout=timeout_s,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace")[-300:]
        raise SceneSoundError(f"stem {source} no es legible para medir onset: {detail}")
    samples = array("h")
    samples.frombytes(proc.stdout)
    if sys.byteorder == "big":
        samples.byteswap()
    if len(samples) < sustain_samples:
        return None
    for index in range(len(samples) - sustain_samples):
        if all(abs(samples[index + offset]) >= threshold for offset in range(sustain_samples)):
            return index / TARGET_SAMPLE_RATE
    return None


def _verify_event_placements(
    plan: SceneSoundPlan,
    mix: Mix,
    samples: Mapping[str, str | Path],
    placement: dict[str, tuple[float, int]],
    *,
    work_dir: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    timeout_s: float,
) -> list[dict[str, Any]]:
    """Mide el onset real de cada evento y lo compara con el previsto.

    Cada cue se ensambla aislado con la MISMA ruta de `render_stems` (adelay al
    frame del timebase) y se mide su onset decodificando el PCM. Un onset
    medido que no coincida con el previsto dentro de la tolerancia falla el
    render: nada se persiste.
    """
    events_by_id = {event.event_id: event for event in mix.all_events()}
    records: list[dict[str, Any]] = []
    for cue in plan.cues:
        event = events_by_id[cue.cue_id]
        isolated = Mix(
            fps=plan.fps,
            duration_s=plan.duration_s,
            stems=[Stem(name=cue.stem, events=[event])],
        )
        rendered = render_stems(
            isolated,
            samples,
            work_dir / re_safe(cue.cue_id),
            ffmpeg_bin=ffmpeg_bin,
            ffprobe_bin=ffprobe_bin,
            timeout_s=timeout_s,
        )
        measured = measure_first_onset_s(
            rendered[0].path, ffmpeg_bin=ffmpeg_bin, timeout_s=timeout_s
        )
        effective, frame = placement[cue.cue_id]
        if measured is None:
            raise SceneSoundError(
                f"cue {cue.cue_id}: el stem renderizado es silencioso; onset no verificable"
            )
        delta_ms = round((measured - effective) * 1000.0, 3)
        if abs(delta_ms) > ONSET_MEASURE_TOLERANCE_S * 1000.0:
            raise SceneSoundError(
                f"cue {cue.cue_id}: onset medido {measured:.4f}s no coincide con el "
                f"previsto {effective:.4f}s (Δ {delta_ms} ms)"
            )
        records.append(
            {
                "cue_id": cue.cue_id,
                "action_id": cue.action_id,
                "stem": cue.stem,
                "intended_onset_s": cue.onset_s,
                "effective_onset_s": effective,
                "frame": frame,
                "measured_onset_s": round(measured, 6),
                "measured_onset_delta_ms": delta_ms,
                "verified": True,
            }
        )
    return records


def re_safe(name: str) -> str:
    """Nombre de directorio seguro y determinista para el render aislado."""
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "cue"


@dataclass(slots=True)
class SceneSoundResult:
    """Resultado del render de sonido por escena, con procedencia persistida."""

    mix: Mix
    mix_sha256: str
    rendered: list[RenderedStem]
    events: list[dict[str, Any]]
    provenance: dict[str, Any]
    provenance_path: Path


def render_scene_sound(
    plan: SceneSoundPlan,
    samples: Mapping[str, str | Path],
    source_video: str | Path,
    out_dir: str | Path,
    *,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> SceneSoundResult:
    """Render acotado del plan: stems editables medidos + procedencia con hashes.

    - Verifica el timebase del video fuente contra ffprobe (fps/duración).
    - Ensambla cada stem con `render_stems` (FFmpeg por argv, PCM s16 estéreo
      48 kHz, onsets alineados al grid de frames, loudnorm EBU R128).
    - Mide el onset real de cada evento y lo compara con el previsto.
    - Persiste `scene_sound_provenance.json` con hashes SHA-256 de la fuente,
      las acciones declaradas, las muestras y los stems de salida.
    """
    plan.validate()
    ff, fp = _resolve_bins(ffmpeg_bin, ffprobe_bin)
    probe = verify_source_timebase(plan, source_video, ffprobe_bin=fp, timeout_s=timeout_s)
    mix, placement = build_mix(plan)
    out_root = Path(out_dir)
    rendered = render_stems(
        mix,
        samples,
        out_root,
        ffmpeg_bin=ff,
        ffprobe_bin=fp,
        timeout_s=timeout_s,
    )
    if not rendered:
        raise SceneSoundError("ningún stem del plan tiene muestras: nada que renderizar")

    with tempfile.TemporaryDirectory(prefix="comfy_scene_sound_verify_") as temp_dir:
        events = _verify_event_placements(
            plan,
            mix,
            samples,
            placement,
            work_dir=Path(temp_dir),
            ffmpeg_bin=ff,
            ffprobe_bin=fp,
            timeout_s=timeout_s,
        )

    # Onset real del PRIMER sonido de cada stem persistido vs el previsto.
    stem_first_onsets: dict[str, float] = {}
    earliest_by_stem: dict[str, float] = {}
    for record in events:
        stem = record["stem"]
        earliest_by_stem[stem] = min(
            earliest_by_stem.get(stem, record["effective_onset_s"]),
            record["effective_onset_s"],
        )
    for entry in rendered:
        first = measure_first_onset_s(entry.path, ffmpeg_bin=ff, timeout_s=timeout_s)
        if first is None:
            raise SceneSoundError(f"stem {entry.stem} renderizado sin sonido medible")
        delta = abs(first - earliest_by_stem[entry.stem])
        if delta > ONSET_MEASURE_TOLERANCE_S:
            raise SceneSoundError(
                f"stem {entry.stem}: primer onset medido {first:.4f}s no coincide con el "
                f"previsto {earliest_by_stem[entry.stem]:.4f}s"
            )
        stem_first_onsets[entry.stem] = round(first, 6)

    source_path = Path(source_video)
    sample_hashes: dict[str, dict[str, Any]] = {}
    licenses_by_asset: dict[str, dict[str, str]] = {}
    for cue in plan.cues:
        licenses_by_asset.setdefault(
            cue.sample_asset_id,
            {
                "license": cue.sample_license or "",
                "attribution": cue.sample_attribution or "",
            },
        )
    for asset_id in sorted(licenses_by_asset):
        sample_path = Path(samples[asset_id])
        if not sample_path.is_file():
            raise SampleValidationError(f"muestra {asset_id} no existe en disco")
        sample_hashes[asset_id] = {
            "name": sample_path.name,
            "sha256": _sha256_file(sample_path),
            **licenses_by_asset[asset_id],
        }
    provenance = {
        "schema": SCENE_SOUND_PROVENANCE_SCHEMA,
        "source_video": {
            "asset_id": plan.source_video_asset_id,
            "name": source_path.name,
            "sha256": _sha256_file(source_path),
        },
        "timebase": {
            "fps": plan.fps,
            "duration_s": plan.duration_s,
            "onset_tolerance_s": plan.onset_tolerance_s,
            "source_probe": {
                "fps": round(probe["fps"], 6),
                "r_frame_rate": probe["r_frame_rate"],
                "duration_s": round(probe["duration_s"], 6),
                "frame_count": probe["frame_count"],
            },
        },
        "actions": [action.to_dict() for action in plan.actions],
        "samples": sample_hashes,
        "plan_sha256": plan_digest(plan),
        "mix_sha256": mix_digest(mix),
        "events": events,
        "stems": [
            {
                "stem": entry.stem,
                "file": entry.path.name,
                "sha256": _sha256_file(entry.path),
                "first_onset_s": stem_first_onsets[entry.stem],
                "earliest_intended_onset_s": earliest_by_stem[entry.stem],
                "event_ids": entry.event_ids,
                "frames": entry.frames,
                "sample_rate": entry.sample_rate,
                "channels": entry.channels,
                "duration_s": entry.duration_s,
                "integrated_loudness_lufs": entry.integrated_loudness_lufs,
                "true_peak_dbfs": entry.true_peak_dbfs,
                "clipped": entry.clipped,
            }
            for entry in rendered
        ],
    }
    provenance_path = out_root / PROVENANCE_FILENAME
    provenance_path.write_text(
        json.dumps(provenance, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return SceneSoundResult(
        mix=mix,
        mix_sha256=mix_digest(mix),
        rendered=rendered,
        events=events,
        provenance=provenance,
        provenance_path=provenance_path,
    )


__all__ = [
    "ACTION_KINDS",
    "DEFAULT_ONSET_TOLERANCE_S",
    "MAX_ACTIONS",
    "MAX_CUES",
    "MAX_ONSET_TOLERANCE_S",
    "ONSET_MEASURE_TOLERANCE_S",
    "PROVENANCE_FILENAME",
    "SOURCE_FPS_TOLERANCE",
    "SOURCE_TIMEBASE_TOLERANCE_S",
    "SCENE_SOUND_PLAN_SCHEMA",
    "SCENE_SOUND_PROVENANCE_SCHEMA",
    "SceneSoundCue",
    "SceneSoundError",
    "SceneSoundPlan",
    "SceneSoundResult",
    "VideoAction",
    "build_mix",
    "frame_of_cue",
    "measure_first_onset_s",
    "plan_digest",
    "probe_source_timebase",
    "render_scene_sound",
    "verify_source_timebase",
]
