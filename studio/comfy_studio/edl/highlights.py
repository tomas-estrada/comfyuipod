"""Generación de highlights y propuestas de EDL desde transcripciones (VAL-PERC-006).

Analiza una transcripción con timestamps por palabra (`whisperx` + opcional `cam++`):
- Elimina segmentos compuestos por muletillas (`eh`, `mmm`, `este`, etc.) y recorta muletillas
  al inicio o final de cada intervención.
- Detecta y suprime pausas largas entre intervenciones (`>= min_pause_sec`).
- Construye un documento EDL versionado (`schema = "comfy_studio.edl.v1"`) validado con
  cortes `[in_sec, out_sec]`, `reason`, `speaker`, `text` y `aspect_ratio` (`16:9` o `9:16`).
"""

from __future__ import annotations

from typing import Any, Literal

from ..asr.whisperx import SPANISH_FILLER_WORDS, transcribe_project_asset
from ..projects import ProjectStore

AspectRatioType = Literal["16:9", "9:16", "1:1", "4:5"]


def _is_word_filler(word_obj: dict[str, Any]) -> bool:
    if bool(word_obj.get("is_filler")):
        return True
    tok = str(word_obj.get("word") or "").strip(".,;:¡!¿?\"'()[]").lower()
    return tok in SPANISH_FILLER_WORDS


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

        if speaker_filter and s_speaker and s_speaker != speaker_filter:
            continue

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
        },
    }
    saved = store.save_edl_version(project_id, edl=edl_doc)
    return saved


build_highlights_cuts_from_transcription = build_edl_cuts_from_transcription
