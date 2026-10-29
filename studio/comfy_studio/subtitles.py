"""Exportación de subtítulos SRT y WebVTT desde una transcripción (M2, `M3-IDEA-032`).

Los datos ya están persistidos (`transcriptions.segments_json`: segmentos con `start`/`end`/
`text`/`speaker` y palabras con timestamps), así que este módulo es **puro formateo**: no
toca modelos, ni ffmpeg, ni la base de datos. Funciona igual con la transcripción simulada
actual y con la real del hito M3, porque solo consume el contrato de segmentos.

Decisiones:
- Se prefiere el **segmento** como unidad de cue: es lo que el usuario ve y edita, y agrupar
  por palabra produciría subtítulos de una palabra por línea.
- El prefijo de hablante es opcional (`include_speakers`) porque en SRT/VTT el nombre dentro
  del texto es lo único portable (no hay campo de hablante en ninguno de los dos formatos).
- Los tiempos se recortan al rango válido y los segmentos degenerados (`end <= start`) se
  descartan en vez de emitir cues de duración cero que algunos reproductores ignoran.
"""

from __future__ import annotations

from typing import Any, Literal

SubtitleFormat = Literal["srt", "vtt"]

MAX_CUE_CHARS = 2000
SRT_TIMECODE = "srt"
VTT_TIMECODE = "vtt"


def _clamp_seconds(value: Any, default: float = 0.0) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return default
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN/inf
        return default
    return max(0.0, seconds)


def format_timecode(seconds: float, *, style: SubtitleFormat = "srt") -> str:
    """Formatea segundos como `HH:MM:SS,mmm` (SRT) o `HH:MM:SS.mmm` (WebVTT)."""
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    sep = "," if style == SRT_TIMECODE else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def _wrap_text(text: str, limit: int = MAX_CUE_CHARS) -> str:
    """Normaliza espacios y acota la longitud de la cue (sin cortar palabras a la mitad)."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    pivot = cut.rfind(" ")
    if pivot > limit // 2:
        cut = cut[:pivot]
    return cut.rstrip() + "…"


def build_cues(
    segments: list[dict[str, Any]],
    *,
    include_speakers: bool = True,
) -> list[dict[str, Any]]:
    """Normaliza los segmentos de una transcripción en cues listos para serializar.

    Descarta segmentos sin texto y los de duración no positiva. Ordena por tiempo de
    inicio, porque un EDL o una reordenación manual puede dejarlos desordenados.
    """
    cues: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        start = _clamp_seconds(seg.get("start", seg.get("start_sec")))
        end_raw = seg.get("end", seg.get("end_sec"))
        end = _clamp_seconds(end_raw, default=start)
        if end <= start:
            continue
        text = _wrap_text(str(seg.get("text") or ""))
        if not text:
            continue
        speaker = str(seg.get("speaker") or "").strip()
        if include_speakers and speaker:
            text = f"{speaker}: {text}"
        cues.append(
            {
                "index": len(cues) + 1,
                "start": start,
                "end": end,
                "text": text,
                "speaker": speaker or None,
                "source_index": idx,
            }
        )
    cues.sort(key=lambda c: (c["start"], c["end"], c["source_index"]))
    for position, cue in enumerate(cues, start=1):
        cue["index"] = position
    return cues


def render_srt(segments: list[dict[str, Any]], *, include_speakers: bool = True) -> str:
    """Serializa los segmentos como SubRip (.srt)."""
    lines: list[str] = []
    for cue in build_cues(segments, include_speakers=include_speakers):
        lines.append(str(cue["index"]))
        lines.append(
            f"{format_timecode(cue['start'], style='srt')} --> "
            f"{format_timecode(cue['end'], style='srt')}"
        )
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)


def render_vtt(segments: list[dict[str, Any]], *, include_speakers: bool = True) -> str:
    """Serializa los segmentos como WebVTT (.vtt)."""
    lines: list[str] = ["WEBVTT", ""]
    for cue in build_cues(segments, include_speakers=include_speakers):
        lines.append(str(cue["index"]))
        lines.append(
            f"{format_timecode(cue['start'], style='vtt')} --> "
            f"{format_timecode(cue['end'], style='vtt')}"
        )
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)


def render_subtitles(
    transcription: dict[str, Any],
    *,
    fmt: SubtitleFormat = "srt",
    include_speakers: bool = True,
) -> str:
    """Renderiza la transcripción en el formato pedido. `fmt` inválido ⇒ `ValueError`."""
    normalized = (fmt or "srt").strip().lower()
    segments = list(transcription.get("segments") or [])
    if normalized == "srt":
        return render_srt(segments, include_speakers=include_speakers)
    if normalized == "vtt":
        return render_vtt(segments, include_speakers=include_speakers)
    raise ValueError(f"Formato de subtítulos no soportado: {fmt!r} (usa 'srt' o 'vtt').")


def subtitle_filename(
    transcription: dict[str, Any],
    *,
    fmt: SubtitleFormat = "srt",
) -> str:
    """Nombre de archivo estable para la descarga (`subtitles-<id>.<ext>`)."""
    tr_id = str(transcription.get("id") or "transcription")
    safe = "".join(ch for ch in tr_id if ch.isalnum() or ch in "-_") or "transcription"
    ext = "vtt" if (fmt or "").strip().lower() == "vtt" else "srt"
    return f"subtitles-{safe}.{ext}"
