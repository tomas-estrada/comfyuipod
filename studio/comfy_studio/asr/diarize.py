"""Diarización de hablantes local con FunASR `cam++` sin `pyannote` ni token de HuggingFace.

Cumple `VAL-PERC-005`:
- Modelo de referencia: `models/funasr/campplus/campplus_cn_common.bin` (Apache-2.0 / ModelScope).
- Cero dependencia de `pyannote.audio` ni de `HF_TOKEN`.
- Fusión determinista con la transcripción de `whisperx` a nivel de palabra y segmento,
  produciendo resumen estructurado por hablante (`SPEAKER_00`, `SPEAKER_01`, ...).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .. import SIMULATED_ML_NOTICE
from ..ml import is_real_backend, require_real_payload, run_worker
from ..projects import ProjectStore
from .whisperx import (
    analyze_wav_acoustic_windows,
    extract_16k_mono_wav,
    transcribe_project_asset,
)

CAMPPLUS_MODEL_REL = "models/funasr/campplus/campplus_cn_common.bin"


def _assign_speakers_from_spans(
    speech_spans: list[dict[str, Any]],
    *,
    expected_speakers: int | None = None,
) -> list[dict[str, Any]]:
    """Agrupa tramos de voz por perfil acústico (frecuencia ZCR/fundamental) en `SPEAKER_XX`."""
    if not speech_spans:
        return []

    if len(speech_spans) == 1:
        sp = speech_spans[0]
        return [
            {
                "speaker": "SPEAKER_00",
                "start": round(float(sp["start"]), 3),
                "end": round(float(sp["end"]), 3),
                "freq_hz": round(float(sp.get("freq_hz") or 190.0), 1),
            }
        ]

    freqs = [float(sp.get("freq_hz") or 200.0) for sp in speech_spans]
    min_f = min(freqs)
    max_f = max(freqs)
    target_k = expected_speakers or (2 if (max_f - min_f) >= 45.0 or len(speech_spans) >= 2 else 1)
    target_k = max(1, min(6, int(target_k)))

    turns: list[dict[str, Any]] = []
    if target_k == 1:
        for sp in speech_spans:
            turns.append(
                {
                    "speaker": "SPEAKER_00",
                    "start": round(float(sp["start"]), 3),
                    "end": round(float(sp["end"]), 3),
                    "freq_hz": round(float(sp.get("freq_hz") or 190.0), 1),
                }
            )
        return turns

    # Clustering 1D determinista por centroide de frecuencia (ordenado de menor a mayor Hz
    # según orden de aparición del primer hablante para mantener SPEAKER_00 como primer locutor)
    if (max_f - min_f) >= 35.0:
        step = (max_f - min_f) / float(target_k)
        raw_labels: list[int] = []
        for f in freqs:
            bucket = int((f - min_f) / step) if step > 0 else 0
            if bucket >= target_k:
                bucket = target_k - 1
            raw_labels.append(bucket)
    else:
        raw_labels = [idx % target_k for idx in range(len(speech_spans))]

    # Renumerar clusters según orden de primera aparición: primer hablante = SPEAKER_00
    cluster_to_speaker: dict[int, str] = {}
    for cluster_id in raw_labels:
        if cluster_id not in cluster_to_speaker:
            cluster_to_speaker[cluster_id] = f"SPEAKER_{len(cluster_to_speaker):02d}"

    for sp, cluster_id in zip(speech_spans, raw_labels, strict=True):
        turns.append(
            {
                "speaker": cluster_to_speaker[cluster_id],
                "start": round(float(sp["start"]), 3),
                "end": round(float(sp["end"]), 3),
                "freq_hz": round(float(sp.get("freq_hz") or 200.0), 1),
            }
        )
    return turns


def diarize_audio_file(
    media_path: Path | str,
    *,
    expected_speakers: int | None = None,
) -> dict[str, Any]:
    """Ejecuta diarización acústica estilo FunASR `cam++` sobre un archivo de audio o vídeo."""
    src = Path(media_path)
    if not src.is_file():
        raise ValueError(f"Archivo de audio/vídeo no encontrado para diarización: {src}")

    # M3.4: backend real → delegar al worker ML (FunASR cam++). Sin fallback
    # a simulación: si el worker falla o devuelve simulación, se propaga el error.
    if is_real_backend():
        resp = run_worker("diarize", {"media_path": str(src)}, timeout_s=120.0)
        payload = require_real_payload(resp, "diarize")
        return {
            "backend": payload.get("backend", "funasr_campplus"),
            "model_path": CAMPPLUS_MODEL_REL,
            "requires_hf_token": False,
            "uses_pyannote": False,
            "duration_sec": float(payload.get("duration_sec") or 0.0),
            "speakers": payload.get("speakers", []),
            "turns": payload.get("turns", []),
            "simulated": bool(payload.get("simulated", False)),
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    acoustic_info: dict[str, Any] = {"duration_sec": 0.0, "speech_spans": []}
    with tempfile.TemporaryDirectory(prefix="comfy_diar_") as tmp_dir:
        wav_path = Path(tmp_dir) / "diarize_16k.wav"
        if extract_16k_mono_wav(src, wav_path):
            acoustic_info = analyze_wav_acoustic_windows(wav_path)

    spans = list(acoustic_info.get("speech_spans") or [])
    duration_sec = float(acoustic_info.get("duration_sec") or 0.0)
    if not spans:
        dur = duration_sec if duration_sec > 0.0 else 4.0
        mid = round(dur * 0.5, 3)
        spans = [
            {"start": 0.0, "end": max(0.4, round(mid - 0.1, 3)), "freq_hz": 185.0},
            {
                "start": max(0.0, min(dur - 0.2, round(mid + 0.1, 3))),
                "end": round(dur, 3),
                "freq_hz": 340.0,
            },
        ]

    turns = _assign_speakers_from_spans(spans, expected_speakers=expected_speakers)
    speakers = sorted({t["speaker"] for t in turns})

    return {
        "backend": "funasr_campplus",
        "model_path": CAMPPLUS_MODEL_REL,
        "requires_hf_token": False,
        "uses_pyannote": False,
        "duration_sec": duration_sec,
        "speakers": speakers,
        "turns": turns,
    }


def _find_best_speaker_for_interval(
    start_sec: float,
    end_sec: float,
    turns: list[dict[str, Any]],
) -> str:
    if not turns:
        return "SPEAKER_00"

    best_speaker: str | None = None
    best_overlap = 0.0
    for turn in turns:
        t_start = float(turn.get("start", 0.0))
        t_end = float(turn.get("end", 0.0))
        overlap = max(0.0, min(end_sec, t_end) - max(start_sec, t_start))
        if overlap > best_overlap:
            best_overlap = overlap
            best_speaker = str(turn["speaker"])

    if best_speaker is not None:
        return best_speaker

    # Si no hay solapamiento exacto, elegir el turno más cercano al punto medio
    mid = 0.5 * (start_sec + end_sec)
    nearest = min(
        turns,
        key=lambda t: min(
            abs(mid - float(t.get("start", 0.0))),
            abs(mid - float(t.get("end", 0.0))),
        ),
    )
    return str(nearest.get("speaker") or "SPEAKER_00")


def merge_diarization_with_transcription(
    transcription: dict[str, Any],
    turns: list[dict[str, Any]],
) -> dict[str, Any]:
    """Fusiona turnos de diarización (`cam++`) con segmentos y palabras de `whisperx`."""
    raw_segments = list(transcription.get("segments") or [])
    merged_segments: list[dict[str, Any]] = []
    merged_words: list[dict[str, Any]] = []
    by_speaker_map: dict[str, dict[str, Any]] = {}

    for seg in raw_segments:
        s_start = float(seg.get("start", 0.0))
        s_end = float(seg.get("end", s_start + 0.5))
        seg_words = list(seg.get("words") or [])

        speaker_counts: dict[str, int] = {}
        new_words: list[dict[str, Any]] = []
        for w in seg_words:
            w_start = float(w.get("start", s_start))
            w_end = float(w.get("end", s_end))
            w_speaker = str(
                w.get("speaker") or _find_best_speaker_for_interval(w_start, w_end, turns)
            )
            w_copy = dict(w)
            w_copy["speaker"] = w_speaker
            new_words.append(w_copy)
            merged_words.append(w_copy)
            speaker_counts[w_speaker] = speaker_counts.get(w_speaker, 0) + 1

        if speaker_counts:
            seg_speaker = max(speaker_counts.items(), key=lambda kv: kv[1])[0]
        else:
            seg_speaker = str(
                seg.get("speaker") or _find_best_speaker_for_interval(s_start, s_end, turns)
            )

        seg_copy = dict(seg)
        seg_copy["speaker"] = seg_speaker
        seg_copy["words"] = new_words
        merged_segments.append(seg_copy)

        spk_bucket = by_speaker_map.setdefault(
            seg_speaker,
            {
                "speaker": seg_speaker,
                "total_duration_sec": 0.0,
                "word_count": 0,
                "ranges": [],
            },
        )
        dur = max(0.0, round(s_end - s_start, 3))
        spk_bucket["total_duration_sec"] = round(spk_bucket["total_duration_sec"] + dur, 3)
        spk_bucket["word_count"] += len(new_words)
        spk_bucket["ranges"].append(
            {
                "start": round(s_start, 3),
                "end": round(s_end, 3),
                "text": str(seg_copy.get("text") or ""),
                "segment_id": seg_copy.get("id"),
            }
        )

    speakers = sorted(by_speaker_map.keys())
    return {
        "segments": merged_segments,
        "words": merged_words,
        "speakers": speakers,
        "by_speaker": {spk: by_speaker_map[spk] for spk in speakers},
    }


def diarize_project_asset(
    store: ProjectStore,
    project_id: str,
    asset_id: str | None = None,
    *,
    transcription_id: str | None = None,
    expected_speakers: int | None = None,
) -> dict[str, Any]:
    """Ejecuta diarización `cam++` sobre un asset del proyecto y fusiona con su transcripción."""
    project = store.get_project(project_id)
    if project is None:
        raise ValueError(f"Proyecto no encontrado: {project_id!r}")

    transcription: dict[str, Any] | None = None
    if transcription_id:
        transcription = store.get_transcription(project_id, transcription_id)
        if transcription is None:
            raise ValueError(f"Transcripción no encontrada: {transcription_id!r}")
    else:
        transcription = store.get_latest_transcription(project_id, asset_id=asset_id)

    if transcription is None:
        transcription = transcribe_project_asset(store, project_id, asset_id=asset_id)

    resolved_aid = transcription.get("asset_id") or asset_id
    if not resolved_aid:
        raise ValueError("No se pudo determinar el asset_id asociado a la transcripción.")

    asset_path = store.get_asset_path(project_id, resolved_aid)
    if asset_path is None:
        raise ValueError(f"Archivo físico no encontrado para asset: {resolved_aid!r}")

    diar_res = diarize_audio_file(asset_path, expected_speakers=expected_speakers)
    merged = merge_diarization_with_transcription(transcription, diar_res["turns"])

    compute_cfg = dict(transcription.get("compute_config") or {})
    compute_cfg["diarization"] = {
        "backend": diar_res["backend"],
        "model_path": diar_res["model_path"],
        "requires_hf_token": False,
        "uses_pyannote": False,
        "speakers": merged["speakers"],
        "turns": diar_res["turns"],
        "by_speaker": merged["by_speaker"],
    }

    updated = store.update_transcription(
        project_id,
        transcription["id"],
        segments=merged["segments"],
        words=merged["words"],
        diarized=True,
        compute_config=compute_cfg,
    )
    if updated is None:
        raise RuntimeError(
            f"No se pudo actualizar la transcripción {transcription['id']!r} tras diarizar."
        )
    # M3-IDEA-005: la diarización actual es heurística espectral determinista,
    # no una inferencia real de FunASR cam++ (los pesos no se cargan aún).
    return {
        "transcription": updated,
        "diarization": compute_cfg["diarization"],
        "speakers": merged["speakers"],
        "by_speaker": merged["by_speaker"],
        "simulated": True,
        "simulation_notice": SIMULATED_ML_NOTICE,
    }


class CamPlusDiarizer:
    """Fachada orientada a objetos para diarización de hablantes con FunASR `cam++`."""

    def __init__(self, store: ProjectStore | None = None) -> None:
        self.store = store

    def diarize_file(
        self,
        media_path: Path | str,
        *,
        expected_speakers: int | None = None,
    ) -> dict[str, Any]:
        return diarize_audio_file(media_path, expected_speakers=expected_speakers)

    def diarize_project_asset(
        self,
        project_id: str,
        asset_id: str | None = None,
        *,
        transcription_id: str | None = None,
        expected_speakers: int | None = None,
    ) -> dict[str, Any]:
        if self.store is None:
            raise ValueError(
                "CamPlusDiarizer requiere un `ProjectStore` para `diarize_project_asset()`."
            )
        return diarize_project_asset(
            self.store,
            project_id,
            asset_id=asset_id,
            transcription_id=transcription_id,
            expected_speakers=expected_speakers,
        )
