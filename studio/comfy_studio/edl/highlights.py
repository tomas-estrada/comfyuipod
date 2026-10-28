"""Generación de highlights y propuestas de EDL desde transcripciones (VAL-PERC-006).

Analiza una transcripción con timestamps por palabra (`whisperx` + opcional `cam++`):
- Elimina segmentos compuestos por muletillas (`eh`, `mmm`, `este`, etc.) y recorta muletillas
  al inicio o final de cada intervención.
- Detecta y suprime pausas largas entre intervenciones (`>= min_pause_sec`).
- Construye un documento EDL versionado (`schema = "comfy_studio.edl.v1"`) validado con
  cortes `[in_sec, out_sec]`, `reason`, `speaker`, `text` y `aspect_ratio` (`16:9` o `9:16`).

M2 (`M3-IDEA-033`), modo híbrido: cuando se pide `scene_cuts`, el corte de highlights se
alinea además con la **energía acústica** real del medio (`analyze_wav_acoustic_windows`,
envolvente RMS por ventanas de 100 ms) y con los **cambios de escena** ya detectados en los
keyframes. La transcripción puede ser simulada; la señal acústica y los límites de escena son
mediciones reales del archivo, así que el recorte deja de depender únicamente de palabras.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Literal

from ..asr.whisperx import (
    SPANISH_FILLER_WORDS,
    analyze_wav_acoustic_windows,
    extract_16k_mono_wav,
    transcribe_project_asset,
)
from ..projects import ProjectStore

AspectRatioType = Literal["16:9", "9:16", "1:1", "4:5"]

# M2: umbrales del modo híbrido. Deliberadamente conservadores: preferimos no mover un
# corte antes que recortar habla por una ventana de energía mal medida.
HYBRID_ENERGY_MIN_RMS = 0.02
HYBRID_SCENE_SNAP_SEC = 0.35


def _is_word_filler(word_obj: dict[str, Any]) -> bool:
    if bool(word_obj.get("is_filler")):
        return True
    tok = str(word_obj.get("word") or "").strip(".,;:¡!¿?\"'()[]").lower()
    return tok in SPANISH_FILLER_WORDS


def analyze_acoustic_energy(media_path: Path | str) -> dict[str, Any]:
    """Mide la envolvente RMS real del medio y devuelve los tramos con habla.

    Usa el mismo extractor 16 kHz mono que la transcripción, así que la medida es
    independiente de que la transcripción sea simulada o real. Ante cualquier fallo
    devuelve `available: False` y el llamador cae al comportamiento sin señal acústica.
    """
    src = Path(media_path)
    if not src.is_file() or shutil.which("ffmpeg") is None:
        return {"available": False, "duration_sec": 0.0, "spans": []}
    with tempfile.TemporaryDirectory(prefix="comfy_hl_ac_") as tmp:
        wav = Path(tmp) / "audio_16k.wav"
        if not extract_16k_mono_wav(src, wav):
            return {"available": False, "duration_sec": 0.0, "spans": []}
        info = analyze_wav_acoustic_windows(wav)
    spans = [
        {"start": round(float(s.get("start", 0.0)), 3), "end": round(float(s.get("end", 0.0)), 3)}
        for s in (info.get("speech_spans") or [])
        if float(s.get("end", 0.0)) > float(s.get("start", 0.0))
    ]
    return {
        "available": bool(spans),
        "duration_sec": float(info.get("duration_sec") or 0.0),
        "spans": spans,
    }


def _snap_to_active_span(
    start: float,
    end: float,
    spans: list[dict[str, float]],
) -> tuple[float, float]:
    """Ajusta un corte al tramo de habla que lo solapa (nunca lo alarga más de lo medido)."""
    for span in spans:
        s_start, s_end = span["start"], span["end"]
        if end <= s_start or start >= s_end:
            continue
        return max(start, s_start), min(end, s_end)
    return start, end


def _snap_to_scene_boundary(
    value: float,
    boundaries: list[float],
    tolerance: float = HYBRID_SCENE_SNAP_SEC,
) -> tuple[float, float | None]:
    """Alinea un instante con el límite de escena más cercano si está dentro de tolerancia."""
    best: float | None = None
    best_delta = tolerance
    for boundary in boundaries:
        delta = abs(boundary - value)
        if delta <= best_delta:
            best = boundary
            best_delta = delta
    if best is None:
        return value, None
    return best, best


def apply_hybrid_refinements(
    cuts: list[dict[str, Any]],
    *,
    acoustic: dict[str, Any],
    scene_boundaries: list[float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Refina los cortes con señal acústica real y límites de escena (M2, `M3-IDEA-033`).

    - Recorta cada corte a la intersección con un tramo de habla medido (si hay señal).
    - Alinea el borde de cada corte con un cambio de escena cuando está a menos de
      `HYBRID_SCENE_SNAP_SEC`.
    - Descarta cortes que quedan por debajo de 0.2 s tras el ajuste.

    Devuelve los cortes refinados y un informe de qué cambió, para que el EDL documente
    por qué difiere de la propuesta puramente textual.
    """
    spans = list(acoustic.get("spans") or []) if acoustic.get("available") else []
    boundaries = sorted(float(b) for b in (scene_boundaries or []))

    refined: list[dict[str, Any]] = []
    report = {
        "acoustic_available": bool(acoustic.get("available")),
        "acoustic_span_count": len(spans),
        "scene_boundary_count": len(boundaries),
        "trimmed_by_acoustic": 0,
        "snapped_to_scene": 0,
        "dropped_too_short": 0,
    }

    for cut in cuts:
        in_sec = round(float(cut.get("in_sec", 0.0)), 3)
        out_sec = round(float(cut.get("out_sec", 0.0)), 3)
        if spans:
            snapped = _snap_to_active_span(in_sec, out_sec, spans)
            if snapped != (in_sec, out_sec):
                report["trimmed_by_acoustic"] += 1
            in_sec, out_sec = snapped
        if boundaries:
            new_in, hit_in = _snap_to_scene_boundary(in_sec, boundaries)
            if hit_in is not None and new_in < out_sec:
                in_sec = new_in
                report["snapped_to_scene"] += 1
            new_out, hit_out = _snap_to_scene_boundary(out_sec, boundaries)
            if hit_out is not None and new_out > in_sec:
                out_sec = new_out
        in_sec = round(in_sec, 3)
        out_sec = round(out_sec, 3)
        if out_sec - in_sec < 0.2:
            report["dropped_too_short"] += 1
            continue
        refined.append({**cut, "in_sec": in_sec, "out_sec": out_sec})

    if not refined:
        return cuts, report
    report["refined"] = True
    return refined, report


def build_edl_cuts_from_transcription(
    transcription: dict[str, Any],
    *,
    source_asset_id: str | None = None,
    source_filename: str | None = None,
    remove_fillers: bool = True,
    min_pause_sec: float = 0.35,
    max_duration_sec: float | None = None,
    speaker_filter: str | None = None,
) -> dict[str, Any]:
    """Construye la lista de cortes de highlights y el informe editorial desde una transcripción."""
    segments = list(transcription.get("segments") or [])
    resolved_asset_id = source_asset_id or transcription.get("asset_id")

    cuts: list[dict[str, Any]] = []
    removed_fillers: list[dict[str, Any]] = []
    removed_pauses: list[dict[str, Any]] = []
    accumulated_dur = 0.0
    prev_end: float | None = None

    for seg in segments:
        s_start = round(float(seg.get("start", 0.0)), 3)
        s_end = round(float(seg.get("end", s_start + 0.5)), 3)
        s_speaker = seg.get("speaker")
        s_text = str(seg.get("text") or "").strip()
        words = list(seg.get("words") or [])

        # M3-SCR-019: un segmento descartado por `speaker_filter` no es una pausa; el
        # informe editorial solo debe contar silencios entre segmentos que sobreviven.
        if speaker_filter and s_speaker and s_speaker != speaker_filter:
            continue

        if prev_end is not None and (s_start - prev_end) >= min_pause_sec:
            removed_pauses.append(
                {
                    "in_sec": round(prev_end, 3),
                    "out_sec": s_start,
                    "duration_sec": round(s_start - prev_end, 3),
                    "reason": "eliminar_pausa",
                }
            )
        prev_end = max(prev_end or 0.0, s_end)

        if remove_fillers:
            if bool(seg.get("is_filler_segment")):
                removed_fillers.append(
                    {
                        "in_sec": s_start,
                        "out_sec": s_end,
                        "text": s_text,
                        "reason": "eliminar_muletilla",
                    }
                )
                continue

            if words:
                filler_words = [w for w in words if _is_word_filler(w)]
                clean_words = [w for w in words if not _is_word_filler(w)]
                # Si >= 40% de las palabras son muletillas o contiene >= 2 muletillas
                if not clean_words or (
                    len(filler_words) >= 2 and len(filler_words) / len(words) >= 0.25
                ):
                    removed_fillers.append(
                        {
                            "in_sec": s_start,
                            "out_sec": s_end,
                            "text": s_text,
                            "reason": "eliminar_muletilla",
                        }
                    )
                    continue

                # Recortar muletillas al inicio y al final del segmento
                first_clean_idx = 0
                while first_clean_idx < len(words) and _is_word_filler(words[first_clean_idx]):
                    w_f = words[first_clean_idx]
                    removed_fillers.append(
                        {
                            "in_sec": round(float(w_f.get("start", s_start)), 3),
                            "out_sec": round(float(w_f.get("end", s_start)), 3),
                            "text": str(w_f.get("word") or ""),
                            "reason": "eliminar_muletilla",
                        }
                    )
                    first_clean_idx += 1

                last_clean_idx = len(words) - 1
                while last_clean_idx >= first_clean_idx and _is_word_filler(words[last_clean_idx]):
                    w_f = words[last_clean_idx]
                    removed_fillers.append(
                        {
                            "in_sec": round(float(w_f.get("start", s_end)), 3),
                            "out_sec": round(float(w_f.get("end", s_end)), 3),
                            "text": str(w_f.get("word") or ""),
                            "reason": "eliminar_muletilla",
                        }
                    )
                    last_clean_idx -= 1

                kept_slice = words[first_clean_idx : last_clean_idx + 1]
                s_start = round(float(kept_slice[0].get("start", s_start)), 3)
                s_end = round(float(kept_slice[-1].get("end", s_end)), 3)
                s_text = " ".join(
                    str(w.get("word") or "") for w in kept_slice if not _is_word_filler(w)
                ).strip()

        if s_end <= s_start:
            s_end = round(s_start + 0.2, 3)

        cut_dur = round(s_end - s_start, 3)
        if max_duration_sec is not None and accumulated_dur + cut_dur > max_duration_sec:
            remaining = round(max_duration_sec - accumulated_dur, 3)
            if remaining >= 0.25:
                s_end = round(s_start + remaining, 3)
                cut_dur = remaining
            else:
                break

        cut_item: dict[str, Any] = {
            "in_sec": s_start,
            "out_sec": s_end,
            "reason": "highlight",
            "text": s_text,
        }
        if resolved_asset_id:
            cut_item["asset_id"] = str(resolved_asset_id)
        if source_filename:
            cut_item["source"] = str(source_filename)
        if s_speaker:
            cut_item["speaker"] = str(s_speaker)

        cuts.append(cut_item)
        accumulated_dur = round(accumulated_dur + cut_dur, 3)

    if not cuts and segments:
        first_seg = segments[0]
        s0 = round(float(first_seg.get("start", 0.0)), 3)
        s1 = round(max(s0 + 0.5, float(first_seg.get("end", s0 + 1.0))), 3)
        fallback_cut: dict[str, Any] = {
            "in_sec": s0,
            "out_sec": s1,
            "reason": "highlight",
            "text": str(first_seg.get("text") or ""),
        }
        if resolved_asset_id:
            fallback_cut["asset_id"] = str(resolved_asset_id)
        if source_filename:
            fallback_cut["source"] = str(source_filename)
        cuts.append(fallback_cut)
        accumulated_dur = round(s1 - s0, 3)

    notes_parts: list[str] = [
        f"Highlights: {len(cuts)} cortes ({accumulated_dur:.2f}s).",
    ]
    if removed_fillers:
        notes_parts.append(f"Muletillas eliminadas: {len(removed_fillers)}.")
    if removed_pauses:
        notes_parts.append(f"Pausas eliminadas: {len(removed_pauses)}.")

    return {
        "cuts": cuts,
        "total_duration_sec": accumulated_dur,
        "removed_fillers": removed_fillers,
        "removed_pauses": removed_pauses,
        "notes": " ".join(notes_parts),
    }


def _collect_scene_boundaries(
    store: ProjectStore,
    project_id: str,
    source_asset_id: str | None,
) -> list[float]:
    """Límites de escena ya detectados en los keyframes del asset de origen.

    No re-ejecuta `ffmpeg`: aprovecha los `shot_start_sec` que la extracción de keyframes
    ya persistió en los metadatos de los assets. Si no hay keyframes, devuelve `[]`.
    """
    if not source_asset_id:
        return []
    boundaries: list[float] = []
    for kf in store.list_assets(project_id, category="keyframes"):
        meta = kf.get("metadata") or {}
        if str(meta.get("source_asset_id") or "") != str(source_asset_id):
            continue
        if meta.get("synthetic"):
            continue
        raw = meta.get("shot_start_sec", meta.get("timestamp_sec"))
        try:
            boundaries.append(round(float(raw), 3))
        except (TypeError, ValueError):
            continue
    return sorted(set(boundaries))


def generate_highlights_edl(
    store: ProjectStore,
    project_id: str,
    *,
    asset_id: str | None = None,
    transcription_id: str | None = None,
    title: str = "Highlights automáticos",
    aspect_ratio: AspectRatioType = "16:9",
    remove_fillers: bool = True,
    min_pause_sec: float = 0.35,
    max_duration_sec: float | None = None,
    speaker_filter: str | None = None,
    hybrid: bool = False,
) -> dict[str, Any]:
    """Genera y persiste una versión de EDL de highlights desde la transcripción del proyecto."""
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

    resolved_aid = asset_id or transcription.get("asset_id")
    source_filename: str | None = None
    if resolved_aid:
        asset_obj = store.get_asset(project_id, resolved_aid)
        if asset_obj is not None:
            source_filename = str(
                asset_obj.get("filename") or asset_obj.get("original_filename") or ""
            )

    built = build_edl_cuts_from_transcription(
        transcription,
        source_asset_id=resolved_aid,
        source_filename=source_filename,
        remove_fillers=remove_fillers,
        min_pause_sec=min_pause_sec,
        max_duration_sec=max_duration_sec,
        speaker_filter=speaker_filter,
    )

    # M2 (`M3-IDEA-033`): refinamiento híbrido con señal real. Solo se activa si se
    # pide, y si no hay forma de medir (sin ffmpeg o sin asset) se deja el EDL textual
    # intacto y se documenta por qué en `editorial_report`.
    hybrid_report: dict[str, Any] | None = None
    if hybrid:
        hybrid_report = {"requested": True, "applied": False}
        asset_path = store.get_asset_path(project_id, resolved_aid) if resolved_aid else None
        if asset_path is None:
            hybrid_report["skipped_reason"] = "El EDL no tiene asset de origen para medir audio."
        else:
            acoustic = analyze_acoustic_energy(asset_path)
            scene_boundaries = _collect_scene_boundaries(store, project_id, resolved_aid)
            refined_cuts, details = apply_hybrid_refinements(
                list(built["cuts"]),
                acoustic=acoustic,
                scene_boundaries=scene_boundaries,
            )
            if details.get("refined"):
                built["cuts"] = refined_cuts
                built["total_duration_sec"] = round(
                    sum(float(c["out_sec"]) - float(c["in_sec"]) for c in refined_cuts), 3
                )
                built["notes"] = (
                    f"{built['notes']} Refinado híbrido: {len(refined_cuts)} cortes "
                    f"(señal acústica: {'sí' if details['acoustic_available'] else 'no'}, "
                    f"escenas: {details['scene_boundary_count']})."
                )
            hybrid_report = {"requested": True, "applied": bool(details.get("refined")), **details}

    edl_doc: dict[str, Any] = {
        "schema": "comfy_studio.edl.v1",
        "title": title,
        "aspect_ratio": aspect_ratio,
        "source_asset_id": resolved_aid,
        "transcription_id": transcription["id"],
        "total_duration_sec": built["total_duration_sec"],
        "cuts": built["cuts"],
        "notes": built["notes"],
        "editorial_report": {
            "removed_fillers": built["removed_fillers"],
            "removed_pauses": built["removed_pauses"],
            "hybrid": hybrid_report,
            # Honestidad: la transcripción de origen es simulada mientras el backend
            # real no esté integrado; el refinamiento acústico sí es una medición real.
            "transcription_simulated": bool(transcription.get("simulated", True)),
        },
    }
    saved = store.save_edl_version(project_id, edl=edl_doc)
    return saved


build_highlights_cuts_from_transcription = build_edl_cuts_from_transcription
