"""Analyze authorized local references and derive provenance-aware recipes.

The analyzer probes local video, detects FFmpeg scene changes, extracts
keyframes, optionally OCRs them, and infers audio onsets. It keeps camera
movement unknown without supporting evidence, never calls external services,
and reports observed/inferred/adapted provenance instead of predicting outcomes.
"""

from __future__ import annotations

import hashlib
import math
import re
import shutil
import sqlite3
import struct
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg


class Origin(StrEnum):
    """De dónde sale cada dato de la receta derivada."""

    OBSERVED = "observed"   # medido de la referencia real
    INFERRED = "inferred"   # deducido por el análisis
    ADAPTED = "adapted"     # ajustado al brief/assets propios


class ConnectorStatus(StrEnum):
    AVAILABLE = "available"
    #: sin credenciales o cuota agotada: opcional, no inventa datos.
    BLOCKED = "blocked"
    #: de pago / cuenta no concedida: nunca se invoca.
    FORBIDDEN_PAID = "forbidden_paid"


@dataclass(slots=True)
class Cut:
    """Un corte detectado en la referencia (tiempo en segundos)."""

    start_s: float
    end_s: float
    origin: Origin = Origin.OBSERVED

    @property
    def duration_s(self) -> float:
        return max(0.0, self.end_s - self.start_s)


@dataclass(slots=True)
class ReferenceAnalysis:
    """Lo que el análisis LOCAL extrae de una referencia autorizada."""

    reference_id: str
    #: shot map: cortes con sus tiempos.
    cuts: list[Cut] = field(default_factory=list)
    #: beat map: instantes (s) de acento rítmico.
    beats: list[float] = field(default_factory=list)
    #: títulos/textos detectados por OCR, con su origen.
    titles: list[dict[str, Any]] = field(default_factory=list)
    #: movimiento de cámara aproximado por corte, or ``unknown`` without evidence.
    camera_moves: list[str] = field(default_factory=list)
    #: Evidencia por corte para los movimientos estimados localmente.
    camera_move_evidence: list[dict[str, Any]] = field(default_factory=list)
    #: de dónde salió la referencia (biblioteca propia / conector permitido).
    source: str = "own_library"
    limitations: list[str] = field(default_factory=list)
    #: hash y duración del medio que sustenta el análisis, cuando se conoce.
    media_sha256: str | None = None
    media_duration_s: float | None = None

    def beat_interval_s(self) -> float | None:
        """Intervalo medio entre beats (tempo), o None si <2 beats."""
        if len(self.beats) < 2:
            return None
        deltas = [b - a for a, b in zip(self.beats, self.beats[1:], strict=False)]
        return round(sum(deltas) / len(deltas), 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "source": self.source,
            "cuts": [
                {"start_s": c.start_s, "end_s": c.end_s, "origin": str(c.origin)}
                for c in self.cuts
            ],
            "beats": list(self.beats),
            "beat_interval_s": self.beat_interval_s(),
            "titles": list(self.titles),
            "camera_moves": list(self.camera_moves),
            "camera_move_evidence": [dict(item) for item in self.camera_move_evidence],
            "media_sha256": self.media_sha256,
            "media_duration_s": self.media_duration_s,
            "limitations": list(self.limitations),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReferenceAnalysis:
        """Restore the JSON-safe analysis cache stored on its owned source asset."""
        cuts = [
            Cut(
                start_s=float(item["start_s"]),
                end_s=float(item["end_s"]),
                origin=Origin(item.get("origin", Origin.OBSERVED)),
            )
            for item in value.get("cuts", [])
        ]
        return cls(
            reference_id=str(value["reference_id"]),
            cuts=cuts,
            beats=[float(item) for item in value.get("beats", [])],
            titles=[dict(item) for item in value.get("titles", [])],
            camera_moves=[str(item) for item in value.get("camera_moves", [])],
            camera_move_evidence=[
                dict(item) for item in value.get("camera_move_evidence", [])
            ],
            source=str(value.get("source", "own_library")),
            limitations=[str(item) for item in value.get("limitations", [])],
            media_sha256=value.get("media_sha256"),
            media_duration_s=(
                float(value["media_duration_s"])
                if value.get("media_duration_s") is not None
                else None
            ),
        )


@dataclass(slots=True)
class RecipeStep:
    """Un paso de la receta editable derivada, con su origen declarado."""

    kind: str          # "shot", "title", "beat_sync", "camera"
    detail: dict[str, Any]
    origin: Origin

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "detail": dict(self.detail), "origin": str(self.origin)}


@dataclass(slots=True)
class EditableRecipe:
    """Receta editable derivada de una referencia y adaptada al brief."""

    reference_id: str
    steps: list[RecipeStep] = field(default_factory=list)
    notes: str = ""

    def by_origin(self, origin: Origin) -> list[RecipeStep]:
        return [s for s in self.steps if s.origin is origin]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "steps": [s.to_dict() for s in self.steps],
            "origin_summary": {
                "observed": len(self.by_origin(Origin.OBSERVED)),
                "inferred": len(self.by_origin(Origin.INFERRED)),
                "adapted": len(self.by_origin(Origin.ADAPTED)),
            },
            "notes": self.notes,
            # A09: no se promete viralidad; el análisis es descriptivo.
            "disclaimer": "análisis descriptivo; no predice viralidad ni garantiza resultados",
        }


def derive_recipe(
    analysis: ReferenceAnalysis,
    *,
    brief_shots: int | None = None,
) -> EditableRecipe:
    """Convierte el análisis en una receta editable adaptada al brief.

    - cada corte conserva su origen observado o inferido;
    - el tempo (beat interval) → un paso `beat_sync` (inferido del ritmo);
    - cada título OCR → un paso `title` (observado, con el texto real);
    - si el brief pide un número de shots distinto, se ADAPTA (origen adapted),
      sin borrar la procedencia.
    """
    recipe = EditableRecipe(reference_id=analysis.reference_id)

    for i, cut in enumerate(analysis.cuts):
        camera_move = analysis.camera_moves[i] if i < len(analysis.camera_moves) else "unknown"
        move_evidence = (
            dict(analysis.camera_move_evidence[i])
            if i < len(analysis.camera_move_evidence)
            else {
                "move": camera_move,
                "origin": "inferred" if camera_move != "unknown" else "unknown",
                "method": "unspecified",
                "confidence": None,
            }
        )
        recipe.steps.append(
            RecipeStep(
                kind="shot",
                detail={
                    "index": i,
                    "duration_s": round(cut.duration_s, 3),
                    "camera_move": camera_move,
                    "camera_move_origin": move_evidence.get("origin", "unknown"),
                    "camera_move_evidence": move_evidence,
                },
                origin=cut.origin,
            )
        )

    interval = analysis.beat_interval_s()
    if interval:
        recipe.steps.append(
            RecipeStep(
                kind="beat_sync",
                detail={"interval_s": interval, "beats": len(analysis.beats)},
                origin=Origin.INFERRED,
            )
        )

    for title in analysis.titles:
        recipe.steps.append(
            RecipeStep(kind="title", detail=dict(title), origin=Origin.OBSERVED)
        )

    # Adaptación al brief: si pide otro número de shots, se ajusta declarándolo.
    observed_shots = len([s for s in recipe.steps if s.kind == "shot"])
    if brief_shots is not None and brief_shots != observed_shots:
        recipe.steps.append(
            RecipeStep(
                kind="shot_count_adaptation",
                detail={"from": observed_shots, "to": brief_shots},
                origin=Origin.ADAPTED,
            )
        )
        recipe.notes = (
            f"adaptado: la referencia tiene {observed_shots} shots, el brief pide {brief_shots}"
        )

    return recipe


def _beat_candidates_from_pcm(pcm_s16le: bytes, sample_rate: int) -> list[float]:
    """Find conservative onset candidates from locally decoded mono PCM."""
    if sample_rate <= 0 or len(pcm_s16le) < 2:
        return []
    complete_samples = pcm_s16le[: len(pcm_s16le) // 2 * 2]
    samples = [sample[0] for sample in struct.iter_unpack("<h", complete_samples)]
    window = max(1, round(sample_rate * 0.02))
    rms: list[float] = []
    for start in range(0, len(samples), window):
        chunk = samples[start : start + window]
        if not chunk:
            continue
        rms.append(math.sqrt(sum(value * value for value in chunk) / len(chunk)))
    if len(rms) < 3:
        return []
    onsets = [max(0.0, current - previous) for previous, current in zip(rms, rms[1:], strict=False)]
    ordered = sorted(onsets)
    median = ordered[len(ordered) // 2]
    deviations = sorted(abs(value - median) for value in onsets)
    mad = deviations[len(deviations) // 2]
    threshold = max(median + 3.0 * mad, max(onsets, default=0.0) * 0.20)
    candidates = [
        i + 1
        for i in range(1, len(onsets) - 1)
        if onsets[i] >= threshold and onsets[i] >= onsets[i - 1] and onsets[i] > onsets[i + 1]
    ]
    min_gap = max(1, round(0.18 / 0.02))
    selected: list[int] = []
    for frame in candidates:
        if not selected or frame - selected[-1] >= min_gap:
            selected.append(frame)
        elif onsets[frame] > onsets[selected[-1]]:
            selected[-1] = frame
    return [round(frame * 0.02, 3) for frame in selected[:256]]


def _scene_cut_times(
    path: Any,
    *,
    threshold: float,
    duration_s: float,
) -> list[float]:
    """Detect FFmpeg scene changes locally; return no claims on tool failure."""
    from .media_bins import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg or not math.isfinite(duration_s) or duration_s <= 0:
        return []
    select_threshold = max(0.05, min(0.95, float(threshold)))
    try:
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "info",
                "-i",
                str(path),
                "-vf",
                f"select='gt(scene\\,{select_threshold:.3f})',showinfo",
                "-an",
                "-f",
                "null",
                "-",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return sorted(
        {
            float(match.group(1))
            for line in result.stderr.splitlines()
            if "Parsed_showinfo" in line
            for match in [re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)]
            if match is not None
            and 0.05 < float(match.group(1)) < duration_s - 0.05
        }
    )


def analyze_reference_asset(
    store: Any,
    project_id: str,
    asset_id: str,
    *,
    scene_threshold: float = 0.25,
    interval_sec: float = 2.0,
) -> ReferenceAnalysis:
    """Analyze a project-owned video offline and preserve observed/inferred limits.

    Keyframe timestamps and OCR text come from local media extraction. The
    interval from each keyframe to the next is only an inferred shot window, not
    an exact cut boundary. Beat candidates are inferred from the decoded audio
    envelope. Missing OCR or motion tooling is reported in ``limitations``.
    """
    # Import lazily: perception.__init__ loads several index backends and may
    # itself be imported by engine registration during application startup.
    from .perception.camera_motion import (
        FRAME_HEIGHT,
        FRAME_WIDTH,
        CameraMotionEstimate,
        decode_grayscale_frames,
        estimate_camera_motion,
    )
    from .perception.keyframes import extract_video_keyframes, probe_media_info

    asset = store.get_asset(project_id, asset_id)
    if asset is None:
        raise LookupError(f"Referencia {asset_id!r} no disponible en el proyecto.")
    if asset.get("kind") != "video":
        raise ValueError("El análisis local de referencia requiere un video.")
    cache_key = "inspiration_analysis_cache_v1"
    cache = (asset.get("metadata") or {}).get(cache_key)
    cached_analysis: ReferenceAnalysis | None = None
    if (
        isinstance(cache, dict)
        and cache.get("schema") == "comfyuipod.reference-analysis-cache/2"
        and cache.get("project_id") == project_id
        and cache.get("asset_id") == asset_id
        and cache.get("asset_version") == asset.get("version")
        and cache.get("source_size_bytes") == asset.get("size_bytes")
        and cache.get("scene_threshold") == scene_threshold
        and cache.get("interval_sec") == interval_sec
        and isinstance(cache.get("analysis"), dict)
    ):
        try:
            candidate = ReferenceAnalysis.from_dict(cache["analysis"])
        except (KeyError, TypeError, ValueError):
            candidate = None
        if (
            candidate is not None
            and candidate.reference_id == asset_id
            and candidate.media_sha256 == cache.get("source_sha256")
        ):
            cached_analysis = candidate

    path = store.get_asset_path(project_id, asset_id)
    if cached_analysis is not None and path is None:
        return cached_analysis
    if path is None:
        raise LookupError(f"Referencia {asset_id!r} no disponible en el proyecto.")
    media_sha256 = _sha256_path(path)
    if cached_analysis is not None and media_sha256 == cached_analysis.media_sha256:
        return cached_analysis
    probe = probe_media_info(path)
    if not probe.get("probe_ok") or not probe.get("has_video"):
        raise ValueError("ffprobe no pudo validar una pista de video real para la referencia.")

    keyframes = extract_video_keyframes(
        store,
        project_id,
        asset_id,
        scene_threshold=scene_threshold,
        interval_sec=max(
            interval_sec,
            float(probe.get("duration_sec") or 0.0) / 29.0,
        ),
    )
    observed_frames = [frame for frame in keyframes if not frame.get("synthetic")]
    if not observed_frames:
        raise ValueError("No se pudieron extraer fotogramas reales de la referencia.")
    ordered_frames = sorted(observed_frames, key=lambda frame: float(frame["timestamp_sec"]))
    duration_s = float(probe.get("duration_sec") or 0.0)
    cut_times = _scene_cut_times(path, threshold=scene_threshold, duration_s=duration_s)
    cuts: list[Cut] = []
    if cut_times:
        boundaries = [0.0, *cut_times, duration_s]
        cuts = [
            Cut(start_s=start_s, end_s=end_s, origin=Origin.OBSERVED)
            for start_s, end_s in zip(boundaries, boundaries[1:], strict=False)
            if math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s
        ]
    if not cuts:
        for frame in ordered_frames:
            start_s = float(frame["shot_start_sec"])
            end_s = float(frame["shot_end_sec"])
            if not (math.isfinite(start_s) and math.isfinite(end_s) and end_s > start_s):
                continue
            cuts.append(Cut(start_s=start_s, end_s=end_s, origin=Origin.INFERRED))
    if not cuts:
        raise ValueError("Los fotogramas reales no contienen intervalos de plano válidos.")
    limitations: list[str] = []
    if not cut_times:
        limitations.append("detección de escenas falló; se usan ventanas inferidas de keyframes")
    if duration_s > 600:
        limitations.append("beats analizados solo en los primeros 600 segundos de audio")
    camera_moves = ["unknown"] * len(cuts)
    camera_move_evidence = [
        CameraMotionEstimate("unknown", 0.0, 0, 0).to_dict() for _ in cuts
    ]
    ffmpeg = find_ffmpeg()
    motion_sampling_failed = ffmpeg is None
    if ffmpeg:
        try:
            motion_frames, _ = decode_grayscale_frames(
                path,
                ffmpeg_bin=ffmpeg,
                duration_s=duration_s,
            )
            for index, cut in enumerate(cuts):
                shot_frames = [
                    frame
                    for timestamp, frame in motion_frames
                    if cut.start_s <= timestamp < cut.end_s
                ]
                estimate = estimate_camera_motion(
                    shot_frames,
                    FRAME_WIDTH,
                    FRAME_HEIGHT,
                )
                camera_moves[index] = estimate.move
                camera_move_evidence[index] = estimate.to_dict()
        except (OSError, subprocess.SubprocessError, ValueError):
            motion_sampling_failed = True
            camera_moves = ["unknown"] * len(cuts)
            camera_move_evidence = [
                CameraMotionEstimate("unknown", 0.0, 0, 0).to_dict() for _ in cuts
            ]
    if motion_sampling_failed:
        limitations.append(
            "estimador local de movimiento de cámara no disponible; se conserva unknown"
        )
    elif "unknown" in camera_moves:
        limitations.append(
            "movimiento de cámara no concluyente en uno o más planos; se conserva unknown"
        )

    titles: list[dict[str, Any]] = []
    tesseract = shutil.which("tesseract")
    if tesseract:
        for frame in ordered_frames:
            frame_path = store.get_asset_path(project_id, str(frame["keyframe_asset_id"]))
            if frame_path is None:
                continue
            try:
                result = subprocess.run(
                    [tesseract, str(frame_path), "stdout", "--psm", "6"],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                limitations.append("OCR local falló en al menos un fotograma")
                break
            text = " ".join((result.stdout or "").split())
            if result.returncode == 0 and text:
                titles.append(
                    {
                        "text": text,
                        "start_s": float(frame["shot_start_sec"]),
                        "end_s": float(frame["shot_end_sec"]),
                        "origin": str(Origin.OBSERVED),
                        "method": "tesseract-local",
                    }
                )
    else:
        limitations.append("OCR no disponible: tesseract no está instalado")

    beats: list[float] = []
    if probe.get("has_audio"):
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            limitations.append("beat map no disponible: ffmpeg no está instalado")
        else:
            try:
                result = subprocess.run(
                    [
                        ffmpeg,
                        "-v",
                        "error",
                        "-i",
                        str(path),
                        "-t",
                        "600",
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "8000",
                        "-f",
                        "s16le",
                        "pipe:1",
                    ],
                    check=False,
                    capture_output=True,
                    timeout=120,
                )
                if result.returncode == 0:
                    beats = _beat_candidates_from_pcm(result.stdout, 8000)
                    if not beats:
                        limitations.append(
                            "no se detectaron acentos de audio por encima del umbral"
                        )
                else:
                    limitations.append("decodificación local de audio falló; beat map vacío")
            except (OSError, subprocess.TimeoutExpired):
                limitations.append("decodificación local de audio falló; beat map vacío")
    else:
        limitations.append("la referencia no contiene audio; beat map no disponible")

    analysis = ReferenceAnalysis(
        reference_id=asset_id,
        cuts=cuts,
        beats=beats,
        titles=titles,
        camera_moves=camera_moves,
        camera_move_evidence=camera_move_evidence,
        source="own_library",
        media_sha256=media_sha256,
        media_duration_s=duration_s or None,
        limitations=limitations,
    )
    try:
        updated = store.save_reference_analysis_cache(
            project_id,
            asset_id,
            {
                "schema": "comfyuipod.reference-analysis-cache/2",
                "project_id": project_id,
                "asset_id": asset_id,
                "asset_version": asset.get("version"),
                "source_size_bytes": asset.get("size_bytes"),
                "source_sha256": media_sha256,
                "scene_threshold": scene_threshold,
                "interval_sec": interval_sec,
                "analysis": analysis.to_dict(),
            },
        )
        if updated is None:
            analysis.limitations.append(
                "el análisis local no pudo guardarse en la caché del activo"
            )
    except (OSError, sqlite3.Error):
        analysis.limitations.append("el análisis local no pudo guardarse en la caché del activo")
    return analysis


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as media_file:
        for chunk in iter(lambda: media_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(slots=True)
class Connector:
    """Un conector de inspiración externo y su estado (nunca de pago activo)."""

    name: str
    status: ConnectorStatus
    note: str = ""

    def is_usable(self) -> bool:
        return self.status is ConnectorStatus.AVAILABLE


def connector_catalog(env: dict[str, str] | None = None) -> list[Connector]:
    """Catálogo de conectores de inspiración con su estado REAL.

    Sin credenciales presentes, un conector queda `blocked` (opcional), no
    inventa datos. Los de pago quedan `forbidden_paid` y nunca se invocan. El
    análisis local sobre la biblioteca propia no necesita ninguno.
    """
    import os

    environment = os.environ if env is None else env
    return [
        Connector(
            name="own_library",
            status=ConnectorStatus.AVAILABLE,
            note="biblioteca propia; modo sin red",
        ),
        Connector(
            name="wikimedia_commons",
            status=(
                ConnectorStatus.AVAILABLE
                if environment.get("STUDIO_WIKIMEDIA_ENABLED")
                else ConnectorStatus.BLOCKED
            ),
            note="API gratuita; opcional, requiere activación explícita",
        ),
        Connector(
            name="heygen_reverse",
            status=ConnectorStatus.FORBIDDEN_PAID,
            note="de pago / cuenta; nunca se invoca (A09)",
        ),
    ]


def assert_no_paid_connector(connector: Connector) -> None:
    """Falla si se intenta usar un conector de pago/no autorizado (A09)."""
    if connector.status is ConnectorStatus.FORBIDDEN_PAID:
        raise RuntimeError(
            f"conector {connector.name} es de pago/no autorizado: no se invoca"
        )


__all__ = [
    "Connector",
    "ConnectorStatus",
    "Cut",
    "EditableRecipe",
    "Origin",
    "RecipeStep",
    "ReferenceAnalysis",
    "assert_no_paid_connector",
    "analyze_reference_asset",
    "connector_catalog",
    "derive_recipe",
]
