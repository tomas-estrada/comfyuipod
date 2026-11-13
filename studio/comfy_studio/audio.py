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

Opera sobre eventos/onsets simbólicos. Verifica la causalidad, el timebase
compartido y la separación de stems, que es lo que A20 pide a nivel de contrato.

C25 añade el ensamble REAL en CPU: las muestras de audio del proyecto se montan
en stems WAV (PCM s16 estéreo 48 kHz) con FFmpeg acotado por argv (sin shell,
con timeouts estrictos), midiendo LUFS integrado y true peak con loudnorm
(EBU R128). Sin generación ni separación por modelo (A20).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg, find_ffprobe


class AudioError(ValueError):
    """Contrato de audio inválido."""


class SampleValidationError(AudioError):
    """La muestra de audio del proyecto no es válida (ilegible, larga, ausente)."""


# Ensamblado real (A20/C25): CPU, FFmpeg acotado, sin generación ni separación
# por modelo. La salida es PCM WAV s16 estéreo a sample rate fijo.
TARGET_SAMPLE_RATE = 48000
#: Límite de eventos por stem: procesamiento acotado y determinista.
MAX_STEM_EVENTS = 64
#: Límite de duración de una muestra individual (ffprobe, en segundos).
MAX_SAMPLE_DURATION_S = 60.0
#: Límite de duración declarada del mix renderizable (segundos).
MAX_MIX_DURATION_S = 3600.0
#: Techo de true peak (dBFS) de la salida medida con loudnorm (EBU R128).
TRUE_PEAK_CEILING_DBFS = -0.1


@dataclass(slots=True)
class SoundEvent:
    """Un evento de sonido en un onset (segundos) con su causa opcional."""

    event_id: str
    onset_s: float
    loudness_lufs: float = -23.0  # EBU R128 de referencia
    #: id de la causa (evento visual/contacto) que origina este sonido, o None.
    caused_by: str | None = None
    #: id del asset de audio (muestra) del proyecto que suena en este evento,
    #: o None en el contrato metadata-only antiguo (W18).
    sample_asset_id: str | None = None

    def validate(self) -> None:
        if not self.event_id.strip():
            raise AudioError("event_id vacío")
        if not math.isfinite(self.onset_s) or self.onset_s < 0:
            raise AudioError(f"evento {self.event_id}: onset inválido")
        if not math.isfinite(self.loudness_lufs):
            raise AudioError(f"evento {self.event_id}: loudness no finito")
        if self.caused_by is not None and not self.caused_by.strip():
            raise AudioError(f"evento {self.event_id}: caused_by vacío")
        if self.sample_asset_id is not None and not str(self.sample_asset_id).strip():
            raise AudioError(f"evento {self.event_id}: sample_asset_id vacío")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "event_id": self.event_id,
            "onset_s": self.onset_s,
            "loudness_lufs": self.loudness_lufs,
            "caused_by": self.caused_by,
        }
        # Solo cuando está declarada: los dicts legacy sin muestras no cambian.
        if self.sample_asset_id is not None:
            data["sample_asset_id"] = self.sample_asset_id
        return data


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
                                sample_asset_id=(
                                    str(event["sample_asset_id"])
                                    if event.get("sample_asset_id") is not None
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


# --- Ensamble real de stems (C25): FFmpeg por argv, CPU, acotado y determinista ----

#: Timeout por defecto de cada invocación de ffmpeg/ffprobe (segundos).
RENDER_TIMEOUT_S = 120.0


def frame_aligned_delay_ms(onset_s: float, fps: int) -> tuple[int, int]:
    """Alinea un onset al grid de frames del timebase compartido.

    Devuelve `(delay_ms, frame)`: el delay en ms para `adelay` y el frame del
    timebase al que cae el onset. Nunca adelanta el sonido: se ancla al frame
    (0.502 s @24fps cae en el frame 12 → 500 ms, no 502 ms).
    """
    if isinstance(fps, bool) or not isinstance(fps, int) or fps <= 0:
        raise AudioError("fps debe ser un entero > 0")
    try:
        onset = float(onset_s)
    except (TypeError, ValueError) as exc:
        raise AudioError("onset_s debe ser finito y >= 0") from exc
    if isinstance(onset_s, bool) or not math.isfinite(onset) or onset < 0:
        raise AudioError("onset_s debe ser finito y >= 0")
    frame = int(round(onset * fps))
    delay_ms = int(round(frame * 1000.0 / fps))
    return delay_ms, frame


def validate_render_bounds(mix: Mix) -> None:
    """Límites de un render acotado y determinista (A20/C25).

    Exige timebase válido, separación real de stems (W19), duración declarada
    dentro del techo y un número de eventos por stem acotado.
    """
    mix.validate()
    assert_separate_stems(mix)
    if mix.duration_s <= 0:
        raise AudioError("duration_s debe declararse (> 0) para renderizar stems")
    if mix.duration_s > MAX_MIX_DURATION_S:
        raise AudioError(
            f"duration_s {mix.duration_s:.0f}s demasiado larga (máx {MAX_MIX_DURATION_S:.0f}s)"
        )
    for stem in mix.stems:
        if len(stem.events) > MAX_STEM_EVENTS:
            raise AudioError(
                f"stem {stem.name}: demasiados eventos ({len(stem.events)} > {MAX_STEM_EVENTS})"
            )


def _resolve_bins(ffmpeg_bin: str | None, ffprobe_bin: str | None) -> tuple[str, str]:
    return (
        ffmpeg_bin or find_ffmpeg() or "ffmpeg",
        ffprobe_bin or find_ffprobe() or "ffprobe",
    )


def _run_argv(cmd: list[str], timeout_s: float, what: str) -> subprocess.CompletedProcess[str]:
    """Ejecuta un subprocesso por argv puro (sin shell) con timeout estricto."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise AudioError(f"{what}: timeout de {timeout_s:.0f}s") from exc
    except OSError as exc:
        raise AudioError(f"{what}: binario no disponible: {exc}") from exc


def _probe_streams(path: Path, ffprobe_bin: str, timeout_s: float) -> dict[str, Any] | None:
    """ffprobe del stream de audio; None si el fichero no es audio legible."""
    proc = _run_argv(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ],
        timeout_s,
        "ffprobe",
    )
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        stream = data["streams"][0]
        return {
            "codec_name": str(stream["codec_name"]),
            "sample_rate": int(stream["sample_rate"]),
            "channels": int(stream["channels"]),
            "duration_s": float(data["format"]["duration"]),
        }
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _validate_sample(asset_id: str, path: str | Path, ffprobe_bin: str, timeout_s: float) -> float:
    """Factibilidad de la muestra: fichero real, legible y dentro del límite."""
    sample = Path(path)
    if not sample.is_file():
        raise SampleValidationError(f"muestra {asset_id} no existe en disco")
    info = _probe_streams(sample, ffprobe_bin, timeout_s)
    if info is None or info["duration_s"] <= 0:
        raise SampleValidationError(f"muestra {asset_id} ({sample}) no es audio legible (ffprobe)")
    if info["duration_s"] > MAX_SAMPLE_DURATION_S:
        raise SampleValidationError(
            f"muestra {asset_id} demasiado larga: "
            f"{info['duration_s']:.1f}s > {MAX_SAMPLE_DURATION_S:.0f}s"
        )
    return info["duration_s"]


def build_stem_assembly_cmd(
    stem: Stem,
    samples: Mapping[str, str | Path],
    out_path: Path,
    fps: int,
    duration_s: float,
    ffmpeg_bin: str | None = None,
) -> list[str]:
    """Construye el argv FFmpeg que ensambla un stem desde sus muestras.

    Determinista: mismo stem/samples → mismo argv. Cada evento con
    `sample_asset_id` es una entrada con `adelay` al frame del timebase;
    `amix` sin normalizar, `apad` y `-t` acotan a la duración declarada; la
    salida es PCM s16 estéreo 48 kHz bitexact (reproducible byte a byte).
    """
    ff, _ = _resolve_bins(ffmpeg_bin, None)
    inputs: list[str] = []
    chains: list[str] = []
    count = 0
    for ev in stem.events:
        asset_id = ev.sample_asset_id
        if asset_id is None:
            continue  # evento metadata-only: no suena en el ensamble
        asset = samples.get(asset_id)
        if asset is None:
            raise SampleValidationError(f"muestra {asset_id} no está en el proyecto")
        inputs += ["-i", str(asset)]
        delay_ms, _frame = frame_aligned_delay_ms(ev.onset_s, fps)
        chains.append(
            f"[{count}:a]aformat=sample_rates={TARGET_SAMPLE_RATE}"
            f":channel_layouts=stereo,adelay={delay_ms}:all=1[s{count}]"
        )
        count += 1
    if count == 0:
        raise AudioError(f"stem {stem.name} sin eventos con muestra: nada que ensamblar")
    mix_in = "".join(f"[s{i}]" for i in range(count))
    graph = (
        ";".join(chains)
        + f";{mix_in}amix=inputs={count}:normalize=0:dropout_transition=0"
        + f",apad=whole_dur={duration_s:.3f}[out]"
    )
    return [
        ff,
        "-hide_banner",
        "-nostdin",
        "-v",
        "error",
        "-y",
        *inputs,
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        "-t",
        f"{duration_s:.3f}",
        "-ar",
        str(TARGET_SAMPLE_RATE),
        "-ac",
        "2",
        "-c:a",
        "pcm_s16le",
        "-bitexact",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        str(out_path),
    ]


def mix_digest(mix: Mix) -> str:
    """Digest determinista del mix serializado (sensible a cualquier edición)."""
    payload = json.dumps(mix.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class RenderedStem:
    """Un stem WAV ensamblado y medido (ffprobe + loudnorm EBU R128)."""

    stem: str
    path: Path
    event_ids: list[str]
    frames: dict[str, int]
    sample_rate: int
    channels: int
    duration_s: float
    integrated_loudness_lufs: float
    true_peak_dbfs: float
    clipped: bool


def verify_rendered_stem(
    path: str | Path,
    *,
    expected_sample_rate: int = TARGET_SAMPLE_RATE,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> dict[str, Any]:
    """Verificación independiente de un WAV renderizado.

    Comprueba que es audio legible (decodificación completa + ffprobe) y que
    cumple el timebase de salida: sample rate, PCM s16 y estéreo.
    """
    ff, fp = _resolve_bins(ffmpeg_bin, ffprobe_bin)
    out = Path(path)
    if not out.is_file():
        raise AudioError(f"stem renderizado {out} no existe")
    decode = _run_argv(
        [ff, "-hide_banner", "-nostdin", "-v", "error", "-i", str(out), "-f", "null", "-"],
        timeout_s,
        "ffmpeg (verificación de stem)",
    )
    if decode.returncode != 0:
        raise AudioError(
            f"stem renderizado {out} no es audio legible: {decode.stderr.strip()[-300:]}"
        )
    info = _probe_streams(out, fp, timeout_s)
    if info is None:
        raise AudioError(f"stem renderizado {out} no es audio legible (ffprobe)")
    if info["sample_rate"] != expected_sample_rate:
        raise AudioError(f"stem {out}: sample rate {info['sample_rate']} != {expected_sample_rate}")
    if info["codec_name"] != "pcm_s16le":
        raise AudioError(f"stem {out}: codec {info['codec_name']} != pcm_s16le")
    if info["channels"] != 2:
        raise AudioError(f"stem {out}: se esperaban 2 canales, hay {info['channels']}")
    return info


def _parse_dbfs(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        if "inf" in text.lower():
            return float("-inf") if text.startswith("-") else float("inf")
        return None


def _measure_loudness(
    path: Path, *, ffmpeg_bin: str | None = None, timeout_s: float = RENDER_TIMEOUT_S
) -> tuple[float, float]:
    """Mide LUFS integrado y true peak con loudnorm (JSON, EBU R128)."""
    ff, _ = _resolve_bins(ffmpeg_bin, None)
    proc = _run_argv(
        [
            ff,
            "-hide_banner",
            "-nostdin",
            "-v",
            "info",
            "-i",
            str(path),
            "-af",
            "loudnorm=print_format=json",
            "-f",
            "null",
            "-",
        ],
        timeout_s,
        "ffmpeg loudnorm",
    )
    if proc.returncode != 0:
        raise AudioError(f"loudnorm falló para {path}: {proc.stderr.strip()[-300:]}")
    blocks = [b for b in re.findall(r"\{[^{}]*\}", proc.stderr) if '"input_i"' in b]
    if not blocks:
        raise AudioError(f"loudnorm no devolvió medición JSON para {path}")
    data = json.loads(blocks[-1])
    integrated = _parse_dbfs(data.get("input_i"))
    true_peak = _parse_dbfs(data.get("input_tp"))
    if integrated is None or true_peak is None:
        raise AudioError(f"loudnorm devolvió valores no numéricos para {path}")
    return integrated, true_peak


def render_stems(
    mix: Mix,
    samples: Mapping[str, str | Path],
    out_dir: str | Path,
    *,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
    timeout_s: float = RENDER_TIMEOUT_S,
) -> list[RenderedStem]:
    """Ensambla cada stem con muestras en un WAV s16 estéreo 48 kHz medido.

    - Stems sin muestras se saltan (el contrato metadata-only sigue intacto).
    - Límites: `validate_render_bounds` + `MAX_SAMPLE_DURATION_S` por muestra;
      una muestra ilegible o ausente falla con `SampleValidationError`.
    - La salida se verifica (`verify_rendered_stem`) y se mide con loudnorm;
      un true peak >= TRUE_PEAK_CEILING_DBFS rechaza el render y borra el
      fichero parcial (nada queda persistido).
    - ffmpeg/ffprobe van por argv (sin shell) con timeout estricto.
    """
    validate_render_bounds(mix)
    ff, fp = _resolve_bins(ffmpeg_bin, ffprobe_bin)
    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    validated: dict[str, float] = {}
    rendered: list[RenderedStem] = []
    for stem in sorted(mix.stems, key=lambda s: s.name):
        sampled = [ev for ev in stem.events if ev.sample_asset_id is not None]
        if not sampled:
            continue  # stem sin muestras: no se rinde
        for ev in sampled:
            if ev.sample_asset_id in validated:
                continue
            asset = samples.get(ev.sample_asset_id)
            if asset is None:
                raise SampleValidationError(f"muestra {ev.sample_asset_id} no está en el proyecto")
            validated[ev.sample_asset_id] = _validate_sample(
                ev.sample_asset_id, asset, fp, timeout_s
            )
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem.name).strip("_") or "stem"
        name_digest = hashlib.sha256(stem.name.encode("utf-8")).hexdigest()[:12]
        out_path = out_root / f"stem_{safe_stem[:64]}_{name_digest}.wav"
        cmd = build_stem_assembly_cmd(
            stem, samples, out_path, fps=mix.fps, duration_s=mix.duration_s, ffmpeg_bin=ff
        )
        proc = _run_argv(cmd, timeout_s, f"ffmpeg (stem {stem.name})")
        if proc.returncode != 0:
            out_path.unlink(missing_ok=True)
            raise AudioError(
                f"ffmpeg falló ensamblando stem {stem.name}: {proc.stderr.strip()[-300:]}"
            )
        info = verify_rendered_stem(
            out_path,
            expected_sample_rate=TARGET_SAMPLE_RATE,
            ffmpeg_bin=ff,
            ffprobe_bin=fp,
            timeout_s=timeout_s,
        )
        integrated, true_peak = _measure_loudness(out_path, ffmpeg_bin=ff, timeout_s=timeout_s)
        if true_peak >= TRUE_PEAK_CEILING_DBFS:
            out_path.unlink(missing_ok=True)
            raise AudioError(
                f"stem {stem.name}: true peak {true_peak:.2f} dBFS supera el techo "
                f"{TRUE_PEAK_CEILING_DBFS} dBFS"
            )
        rendered.append(
            RenderedStem(
                stem=stem.name,
                path=out_path,
                event_ids=[ev.event_id for ev in sampled],
                frames={ev.event_id: mix.frame_of(ev.onset_s) for ev in sampled},
                sample_rate=info["sample_rate"],
                channels=info["channels"],
                duration_s=info["duration_s"],
                integrated_loudness_lufs=integrated,
                true_peak_dbfs=true_peak,
                clipped=False,
            )
        )
    return rendered


__all__ = [
    "MAX_SAMPLE_DURATION_S",
    "MAX_STEM_EVENTS",
    "MAX_MIX_DURATION_S",
    "RENDER_TIMEOUT_S",
    "TARGET_SAMPLE_RATE",
    "TRUE_PEAK_CEILING_DBFS",
    "AudioError",
    "Mix",
    "RenderedStem",
    "SampleValidationError",
    "SoundEvent",
    "Stem",
    "assert_separate_stems",
    "build_stem_assembly_cmd",
    "delete_cause",
    "frame_aligned_delay_ms",
    "mix_digest",
    "render_stems",
    "validate_render_bounds",
    "verify_rendered_stem",
]
