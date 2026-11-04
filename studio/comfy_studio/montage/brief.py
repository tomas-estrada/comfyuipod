"""Montaje asistido y verificable desde un brief en lenguaje natural (M7).

Dado un brief (p. ej. "quiero los momentos donde Ana explica el incendio, del
segundo 2 al 4"), el módulo recupera momentos relevantes del proyecto cruzando
tres modalidades ya existentes:

- **imagen**: búsqueda semántica texto→plano con el índice CLIP (`find_shots_text`);
- **diálogo**: búsqueda por términos (idf) sobre las transcripciones persistidas;
- **persona**: búsqueda por identidad facial (`find_shots_face`) cuando el brief
  menciona a una identidad registrada (o se pasa `person`).

Cada candidato se convierte en un corte con `asset_id` + timecodes de origen y un
bloque `evidence` que referencia el material real del proyecto (shot_id /
keyframe_asset_id / transcription_id / identity_id, términos coincidentes y
similitud). Los timecodes se verifican y acotan contra la duración real del asset
medida con `ffprobe` (`probe_media_info`); si el probe falla el corte se marca
`timecode_verified: false` en vez de presentarse como verificado.

El resultado se persiste como una versión de EDL (`schema = "comfy_studio.edl.v1"`,
`kind = "montage"`) con los cortes y su evidencia, de modo que el borrador se puede
renderizar después con el ejecutor real existente (`execute_edl_ffmpeg`, verificado
con ffprobe) o editar en el panel. Nada se renderiza salvo que se pida `render=true`.

Honestidad: mientras los backends ML estén en modo simulación, la respuesta lleva
`simulated: true` + `simulation_notice` y cada evidencia lo marca; la lógica de
montaje (recuperación por términos, fusión, empaquetado, EDL y render) es real y
determinista, igual que la verificación de timecodes con ffprobe.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .. import SIMULATED_ML_NOTICE
from ..edl.ffmpeg_edl import MAX_EDL_CUTS
from ..perception.keyframes import probe_media_info

AspectRatioType = Literal["16:9", "9:16", "1:1", "4:5"]

MONTAGE_EDL_KIND = "montage"

DEFAULT_MAX_CUTS = 12
MAX_CUTS_LIMIT = 48
DEFAULT_MAX_SEGMENT_SEC = 30.0
MERGE_IOU_THRESHOLD = 0.25
MERGE_EVIDENCE_BONUS = 0.15

STOPWORDS = frozenset(
    """
    el la los las un una uno una y o u e ni que de del al a en con por para sin sobre
    entre desde hasta ser estar tener haber hace asi como lo su sus mi mis tu tus se
    este esta estos estas ese esa esos esas ya muy mas menos mucho como cuando donde
    the a an and or but if then of at by for with about to from in on off over under
    is are was were be been this that these those it its as not no
    """.split()
)

_TIME_RANGE_RE = re.compile(
    r"(?:del|desde(?:\s+el)?|entre(?:\s+el)?)\s*segundos?\s*(\d+(?:[.,]\d+)?)\s*"
    r"(?:al|a|hasta|-|–)\s*(?:el\s*)?(?:segundos?\s*)?(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


def _normalize_token(text: str) -> str:
    lowered = (text or "").strip().lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def tokenize_terms(text: str) -> list[str]:
    """Tokens normalizados (sin acentos, sin stopwords) en orden de aparición."""
    terms: list[str] = []
    for raw in re.split(r"[^\w]+", (text or "").lower(), flags=re.UNICODE):
        tok = _normalize_token(raw)
        if len(tok) >= 3 and tok not in STOPWORDS and tok not in terms:
            terms.append(tok)
    return terms


def parse_brief(
    brief: str,
    identity_names: list[str] | None = None,
) -> dict[str, Any]:
    """Extrae términos, personas mencionadas y rango temporal del brief.

    - `persons`: nombres de identidades registradas que aparecen en el brief
      (coincidencia por palabra completa, insensible a acentos y mayúsculas).
    - `time_range_sec`: rango "del segundo X al Y" si aparece en el brief.
    """
    cleaned = (brief or "").strip()
    time_range_match = _TIME_RANGE_RE.search(cleaned)
    terms_source = cleaned
    if time_range_match:
        terms_source = cleaned[: time_range_match.start()] + cleaned[time_range_match.end() :]
    normalized = _normalize_token(cleaned)
    persons: list[str] = []
    for name in identity_names or []:
        name_norm = _normalize_token(name)
        if len(name_norm) >= 2 and re.search(rf"\b{re.escape(name_norm)}\b", normalized):
            persons.append(name)
    time_range: tuple[float, float] | None = None
    if time_range_match:
        lo = float(time_range_match.group(1).replace(",", "."))
        hi = float(time_range_match.group(2).replace(",", "."))
        if 0.0 <= lo < hi:
            time_range = (lo, hi)
    return {
        "terms": tokenize_terms(terms_source),
        "persons": persons,
        "time_range_sec": list(time_range) if time_range else None,
    }


class MontageBriefRequest(BaseModel):
    """Petición de montaje asistido desde brief (POST .../montage/from-brief)."""

    brief: str = Field(..., min_length=3, max_length=2000)
    title: str = Field(default="Montaje desde brief", min_length=1, max_length=200)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|4:5)$")
    target_duration_sec: float | None = Field(default=None, gt=0.0, le=3600.0)
    max_cuts: int = Field(default=DEFAULT_MAX_CUTS, ge=1, le=MAX_CUTS_LIMIT)
    min_segment_sec: float = Field(default=1.0, ge=0.2, le=120.0)
    max_segment_sec: float = Field(default=DEFAULT_MAX_SEGMENT_SEC, ge=0.5, le=600.0)
    asset_ids: list[str] | None = Field(default=None, max_length=20)
    person: str | None = Field(default=None, min_length=1, max_length=120)
    time_range_sec: list[float] | None = Field(default=None, min_length=2, max_length=2)
    render: bool = Field(
        default=False,
        description=(
            "Si es true, renderiza el borrador con ffmpeg (verificado con ffprobe) "
            "tras guardar el EDL. Acción con costo: requiere opt-in explícito."
        ),
    )
    fps: int = Field(default=24, ge=12, le=60)

    @field_validator("time_range_sec")
    @classmethod
    def _validate_time_range(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return None
        lo, hi = float(value[0]), float(value[1])
        if not (0.0 <= lo < hi):
            raise ValueError("time_range_sec debe cumplir 0 <= inicio < fin")
        return [lo, hi]


MontagePlanResult = dict[str, Any]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _iou(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(1e-9, (a1 - a0) + (b1 - b0) - inter)
    return inter / union


def retrieve_transcript_candidates(
    store: Any,
    project_id: str,
    terms: list[str],
    *,
    asset_ids: set[str] | None = None,
    time_range: tuple[float, float] | None = None,
    max_segments: int = 200,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recupera segmentos de transcripción por coincidencia de términos (idf).

    Devuelve candidatos con score de cobertura (0..1) = idf de los términos del
    brief presentes en el segmento / idf total del brief, y `matched_terms` como
    evidencia trazable al `transcription_id` y `segment_id` reales.
    """
    if not terms:
        return [], {"transcript_segments": 0, "matched": 0, "skipped": "sin_términos"}
    transcriptions = store.list_transcriptions(project_id)
    all_segments: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    for tr in transcriptions:
        if asset_ids is not None and tr.get("asset_id") not in asset_ids:
            continue
        for idx, seg in enumerate(tr.get("segments") or []):
            if isinstance(seg, dict) and str(seg.get("text") or "").strip():
                all_segments.append((tr, seg, idx))
    if not all_segments:
        return [], {"transcript_segments": 0, "matched": 0, "skipped": "sin_transcripciones"}

    doc_freq: dict[str, int] = {}
    seg_tokens: list[set[str]] = []
    for _tr, seg, _idx in all_segments:
        toks = set(tokenize_terms(str(seg.get("text") or "")))
        seg_tokens.append(toks)
        for tok in toks:
            doc_freq[tok] = doc_freq.get(tok, 0) + 1
    n_docs = max(1, len(all_segments))
    idf = {t: math.log((n_docs + 1.0) / (df + 1.0)) + 1.0 for t, df in doc_freq.items()}
    term_idf = {t: idf.get(t, math.log(n_docs + 1.0) + 1.0) for t in terms}
    total_idf = sum(term_idf.values()) or 1.0

    candidates: list[dict[str, Any]] = []
    for (tr, seg, idx), toks in zip(all_segments, seg_tokens, strict=True):
        matched = [t for t in terms if t in toks]
        if not matched:
            continue
        start = round(float(seg.get("start", 0.0)), 3)
        end = round(float(seg.get("end", start + 0.5)), 3)
        if end <= start:
            continue
        if time_range is not None:
            lo, hi = time_range
            if end <= lo or start >= hi:
                continue
            start, end = max(start, lo), min(end, hi)
            if end <= start:
                continue
        score = round(min(1.0, sum(term_idf[t] for t in matched) / total_idf), 4)
        asset_id = str(tr.get("asset_id") or "")
        if asset_ids is not None and asset_id not in asset_ids:
            continue
        candidates.append(
            {
                "asset_id": asset_id,
                "in_sec": start,
                "out_sec": end,
                "score": score,
                "text": str(seg.get("text") or "").strip(),
                "speaker": seg.get("speaker"),
                "evidence": [
                    {
                        "mode": "transcript",
                        "transcription_id": tr.get("id"),
                        "segment_id": seg.get("id") or f"seg-{idx + 1:03d}",
                        "segment_index": idx,
                        "matched_terms": matched,
                        "score": score,
                        "speaker": seg.get("speaker"),
                        "text": str(seg.get("text") or "").strip(),
                    }
                ],
            }
        )
    candidates.sort(key=lambda c: (-c["score"], c["asset_id"], c["in_sec"]))
    report = {
        "transcript_segments": len(all_segments),
        "matched": len(candidates),
        "truncated": max(0, len(candidates) - max_segments),
    }
    return candidates[:max_segments], report


def retrieve_visual_candidates(
    clip_index: Any,
    project_id: str,
    brief: str,
    *,
    asset_ids: set[str] | None = None,
    limit: int = 30,
    time_range: tuple[float, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recupera planos del índice CLIP por similitud semántica con el brief."""
    if clip_index is None:
        return [], {"visual_shots": 0, "skipped": "sin_indice_clip"}
    if asset_ids:
        shots: list[dict[str, Any]] = []
        for aid in sorted(asset_ids):
            shots.extend(
                clip_index.find_shots_text(project_id, brief, limit=limit, asset_id=aid)
            )
    else:
        shots = clip_index.find_shots_text(project_id, brief, limit=limit)
    candidates: list[dict[str, Any]] = []
    seen_shots: set[str] = set()
    for shot in shots:
        shot_id = str(shot.get("shot_id") or "")
        if not shot_id or shot_id in seen_shots:
            continue
        seen_shots.add(shot_id)
        asset_id = str(shot.get("source_asset_id") or "")
        start = round(float(shot.get("shot_start_sec", shot.get("timestamp_sec", 0.0))), 3)
        end = round(float(shot.get("shot_end_sec", start + 2.0)), 3)
        if end <= start:
            end = round(start + 2.0, 3)
        if time_range is not None:
            lo, hi = time_range
            if end <= lo or start >= hi:
                continue
            start, end = max(start, lo), min(end, hi)
            if end <= start:
                continue
        sim = float(shot.get("similarity", 0.0))
        candidates.append(
            {
                "asset_id": asset_id,
                "in_sec": start,
                "out_sec": end,
                "score": round(_clamp(sim, 0.0, 1.0), 4),
                "text": None,
                "speaker": None,
                "evidence": [
                    {
                        "mode": "visual",
                        "shot_id": shot_id,
                        "keyframe_asset_id": shot.get("keyframe_asset_id"),
                        "timestamp_sec": float(shot.get("timestamp_sec", 0.0)),
                        "similarity": round(sim, 4),
                        "caption": shot.get("caption") or "",
                        "visual_tags": list(shot.get("visual_tags") or []),
                    }
                ],
            }
        )
    candidates.sort(key=lambda c: (-c["score"], c["asset_id"], c["in_sec"]))
    return candidates, {"visual_shots": len(candidates)}


def retrieve_identity_candidates(
    face_index: Any,
    project_id: str,
    *,
    identity_id: str | None = None,
    identity_name: str | None = None,
    text_query: str | None = None,
    limit: int = 30,
    time_range: tuple[float, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recupera planos donde aparece la identidad indicada (texto ∩ persona)."""
    if face_index is None or not (identity_id or identity_name):
        return [], {"identity_shots": 0, "skipped": "sin_indice_facial"}
    shots = face_index.find_shots_face(
        project_id,
        identity_id=identity_id,
        identity_name=identity_name,
        text_query=text_query,
        limit=limit,
    )
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for shot in shots:
        kf = str(shot.get("keyframe_asset_id") or "")
        if not kf or kf in seen:
            continue
        seen.add(kf)
        asset_id = str(shot.get("source_asset_id") or "")
        start = round(float(shot.get("shot_start_sec", shot.get("timestamp_sec", 0.0))), 3)
        end = round(float(shot.get("shot_end_sec", start + 2.0)), 3)
        if end <= start:
            end = round(start + 2.0, 3)
        if time_range is not None:
            lo, hi = time_range
            if end <= lo or start >= hi:
                continue
            start, end = max(start, lo), min(end, hi)
            if end <= start:
                continue
        sim = float(shot.get("similarity", shot.get("face_similarity", 0.0)))
        candidates.append(
            {
                "asset_id": asset_id,
                "in_sec": start,
                "out_sec": end,
                "score": round(_clamp(sim, 0.0, 1.0), 4),
                "text": None,
                "speaker": None,
                "evidence": [
                    {
                        "mode": "identity",
                        "identity_id": shot.get("identity_id"),
                        "identity_name": shot.get("person_name"),
                        "keyframe_asset_id": kf,
                        "timestamp_sec": float(shot.get("timestamp_sec", 0.0)),
                        "similarity": round(sim, 4),
                    }
                ],
            }
        )
    candidates.sort(key=lambda c: (-c["score"], c["asset_id"], c["in_sec"]))
    return candidates, {"identity_shots": len(candidates)}


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fusiona candidatos solapados del mismo asset sumando su evidencia.

    Dos candidatos del mismo asset con IoU >= `MERGE_IOU_THRESHOLD` se unen en
    uno: el timecode resultante es la envolvente y el score crece con cada modo
    de evidencia adicional (bonus acotado a 1.0).
    """
    ordered = sorted(candidates, key=lambda c: (c["asset_id"], c["in_sec"]))
    merged: list[dict[str, Any]] = []
    for cand in ordered:
        target: dict[str, Any] | None = None
        for existing in merged:
            if existing["asset_id"] != cand["asset_id"]:
                continue
            if _iou(existing["in_sec"], existing["out_sec"], cand["in_sec"], cand["out_sec"]) >= (
                MERGE_IOU_THRESHOLD
            ):
                target = existing
                break
        if target is None:
            merged.append(
                {
                    "asset_id": cand["asset_id"],
                    "in_sec": cand["in_sec"],
                    "out_sec": cand["out_sec"],
                    "score": cand["score"],
                    "text": cand.get("text"),
                    "speaker": cand.get("speaker"),
                    "evidence": list(cand["evidence"]),
                }
            )
            continue
        new_in = min(target["in_sec"], cand["in_sec"])
        new_out = max(target["out_sec"], cand["out_sec"])
        modes = {e.get("mode") for e in target["evidence"]}
        bonus = MERGE_EVIDENCE_BONUS * sum(
            1 for e in cand["evidence"] if e.get("mode") not in modes
        )
        target["in_sec"], target["out_sec"] = new_in, new_out
        target["score"] = round(min(1.0, max(target["score"], cand["score"]) + bonus), 4)
        target["evidence"].extend(cand["evidence"])
        if cand.get("text") and not target.get("text"):
            target["text"] = cand["text"]
        if cand.get("speaker") and not target.get("speaker"):
            target["speaker"] = cand["speaker"]
    merged.sort(key=lambda c: (-c["score"], c["asset_id"], c["in_sec"]))
    return merged


def probe_source_durations(
    store: Any,
    project_id: str,
    asset_ids: list[str],
    *,
    ffprobe_timeout_s: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Mide la duración real de cada asset fuente con ffprobe (verificación)."""
    durations: dict[str, dict[str, Any]] = {}
    for aid in asset_ids:
        path = store.get_asset_path(project_id, aid)
        if path is None:
            durations[aid] = {"duration_sec": 0.0, "probe_ok": False, "reason": "sin_ruta"}
            continue
        info = probe_media_info(path, timeout_s=ffprobe_timeout_s)
        durations[aid] = {
            "duration_sec": float(info.get("duration_sec") or 0.0),
            "probe_ok": bool(info.get("probe_ok")),
        }
    return durations


def select_cuts(
    candidates: list[dict[str, Any]],
    durations: dict[str, dict[str, Any]],
    *,
    target_duration_sec: float | None,
    max_cuts: int,
    min_segment_sec: float,
    max_segment_sec: float,
    time_range: tuple[float, float] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Empaqueta candidatos (ordenados por score) en cortes no solapados.

    - Recorta cada segmento a `max_segment_sec` y lo acota a la duración real del
      asset medida con ffprobe; descarta los que quedan por debajo de
      `min_segment_sec` o fuera de `time_range`.
    - Si hay `target_duration_sec`, recorta el último corte para no pasarse.
    """
    cuts: list[dict[str, Any]] = []
    report = {
        "candidates": len(candidates),
        "selected": 0,
        "dropped_too_short": 0,
        "dropped_unverified_duration": 0,
        "dropped_overlap": 0,
        "trimmed_to_duration": 0,
        "clamped_by_probe": 0,
        "timecode_verified": True,
    }
    accumulated = 0.0
    for cand in sorted(candidates, key=lambda c: (-c["score"], c["asset_id"], c["in_sec"])):
        if len(cuts) >= min(max_cuts, MAX_EDL_CUTS):
            break
        asset_id = cand["asset_id"]
        dur_info = durations.get(asset_id) or {"duration_sec": 0.0, "probe_ok": False}
        in_sec = round(float(cand["in_sec"]), 3)
        out_sec = round(float(cand["out_sec"]), 3)
        if time_range is not None:
            lo, hi = time_range
            in_sec, out_sec = max(in_sec, lo), min(out_sec, hi)
        if dur_info.get("probe_ok"):
            max_dur = float(dur_info.get("duration_sec") or 0.0)
            new_out = min(out_sec, max_dur)
            if new_out < out_sec:
                report["clamped_by_probe"] += 1
            out_sec = new_out
            in_sec = min(in_sec, max_dur)
        else:
            report["timecode_verified"] = False
        if out_sec - in_sec > max_segment_sec:
            out_sec = round(in_sec + max_segment_sec, 3)
        if out_sec - in_sec < min_segment_sec:
            report["dropped_too_short"] += 1
            continue
        if any(
            c["asset_id"] == asset_id
            and _iou(c["in_sec"], c["out_sec"], in_sec, out_sec) > 0.0
            for c in cuts
        ):
            report["dropped_overlap"] += 1
            continue
        seg_dur = round(out_sec - in_sec, 3)
        if target_duration_sec is not None and accumulated + seg_dur > target_duration_sec:
            remaining = round(target_duration_sec - accumulated, 3)
            if remaining >= min_segment_sec:
                out_sec = round(in_sec + remaining, 3)
                seg_dur = remaining
                report["trimmed_to_duration"] += 1
            else:
                continue
        evidence = []
        for ev in cand["evidence"]:
            entry = dict(ev)
            entry["timecode_verified"] = bool(dur_info.get("probe_ok"))
            evidence.append(entry)
        cuts.append(
            {
                "in_sec": in_sec,
                "out_sec": out_sec,
                "asset_id": asset_id,
                "reason": "brief_match",
                "text": cand.get("text"),
                "speaker": cand.get("speaker"),
                "score": cand["score"],
                "evidence": evidence,
            }
        )
        accumulated = round(accumulated + seg_dur, 3)
        report["selected"] += 1
        if target_duration_sec is not None and accumulated >= target_duration_sec:
            break
    cuts.sort(key=lambda c: (c["asset_id"], c["in_sec"]))
    return cuts, report


def plan_montage_from_brief(
    store: Any,
    project_id: str,
    *,
    brief: str,
    clip_index: Any = None,
    face_index: Any = None,
    perception_available: bool = True,
    perception_stale: bool = False,
    ml_simulated: bool = True,
    title: str = "Montaje desde brief",
    aspect_ratio: AspectRatioType = "16:9",
    target_duration_sec: float | None = None,
    max_cuts: int = DEFAULT_MAX_CUTS,
    min_segment_sec: float = 1.0,
    max_segment_sec: float = DEFAULT_MAX_SEGMENT_SEC,
    asset_ids: list[str] | None = None,
    person: str | None = None,
    time_range_sec: list[float] | None = None,
    ffprobe_timeout_s: float = 20.0,
) -> MontagePlanResult:
    """Planifica y persiste un EDL de montaje desde un brief; no renderiza nada.

    Devuelve el EDL guardado (con evidencia por corte) más el informe de
    recuperación. Lanza `ValueError` ante proyecto/brief/assets inválidos.
    """
    project = store.get_project(project_id)
    if project is None:
        raise ValueError(f"Proyecto no encontrado: {project_id!r}")
    cleaned_brief = (brief or "").strip()
    if len(cleaned_brief) < 3:
        raise ValueError("El brief debe tener al menos 3 caracteres.")

    resolved_asset_ids: set[str] | None = None
    if asset_ids:
        existing = {a["id"] for a in store.list_assets(project_id)}
        unknown = [aid for aid in asset_ids if aid not in existing]
        if unknown:
            raise ValueError(f"Assets no encontrados en el proyecto: {unknown!r}")
        resolved_asset_ids = set(asset_ids)

    identities = store.list_identities(project_id)
    identity_names = [str(i.get("name") or "") for i in identities]
    parsed = parse_brief(cleaned_brief, identity_names)
    persons = list(parsed["persons"])
    if person:
        person_clean = person.strip()
        if person_clean not in persons:
            persons.append(person_clean)

    time_range: tuple[float, float] | None = None
    if time_range_sec:
        time_range = (float(time_range_sec[0]), float(time_range_sec[1]))
    elif parsed["time_range_sec"]:
        time_range = (float(parsed["time_range_sec"][0]), float(parsed["time_range_sec"][1]))

    terms = parsed["terms"]

    transcript_candidates, transcript_report = retrieve_transcript_candidates(
        store,
        project_id,
        terms,
        asset_ids=resolved_asset_ids,
        time_range=time_range,
    )

    use_visual = bool(perception_available) and clip_index is not None and not perception_stale
    visual_candidates: list[dict[str, Any]] = []
    visual_report: dict[str, Any] = {"visual_shots": 0, "skipped": "no_disponible"}
    if use_visual:
        visual_candidates, visual_report = retrieve_visual_candidates(
            clip_index,
            project_id,
            cleaned_brief,
            asset_ids=resolved_asset_ids,
            time_range=time_range,
        )
    elif perception_stale:
        visual_report = {"visual_shots": 0, "skipped": "indice_stale"}

    identity_candidates: list[dict[str, Any]] = []
    identity_report: dict[str, Any] = {"identity_shots": 0, "skipped": "sin_persona"}
    if use_visual and face_index is not None and persons:
        for name in persons:
            identity = store.get_identity_by_name(project_id, name)
            try:
                cand, rep = retrieve_identity_candidates(
                    face_index,
                    project_id,
                    identity_id=str(identity["id"]) if identity else None,
                    identity_name=name,
                    text_query=cleaned_brief,
                    time_range=time_range,
                )
            except ValueError as exc:
                identity_report = {
                    "identity_shots": identity_report.get("identity_shots", 0),
                    "skipped": f"identidad_no_resuelta: {exc}",
                }
                continue
            identity_candidates.extend(cand)
            identity_report["identity_shots"] += rep.get("identity_shots", 0)
        if not identity_candidates:
            identity_report = {"identity_shots": 0, "skipped": "sin_coincidencias"}
    elif persons and not use_visual:
        identity_report = {"identity_shots": 0, "skipped": "percepcion_no_disponible"}

    all_candidates = transcript_candidates + visual_candidates + identity_candidates
    merged = merge_candidates(all_candidates)

    involved_assets = sorted({c["asset_id"] for c in merged if c.get("asset_id")})
    durations = probe_source_durations(
        store, project_id, involved_assets, ffprobe_timeout_s=ffprobe_timeout_s
    )
    cuts, selection_report = select_cuts(
        merged,
        durations,
        target_duration_sec=target_duration_sec,
        max_cuts=max_cuts,
        min_segment_sec=min_segment_sec,
        max_segment_sec=max_segment_sec,
        time_range=time_range,
    )
    if not cuts:
        raise ValueError(
            "El brief no produjo ningún corte con evidencia en el proyecto "
            f"(términos: {terms!r}, personas: {persons!r}). Indexa el proyecto o "
            "transcribe un asset primero."
        )

    for cut in cuts:
        for ev in cut["evidence"]:
            ev["simulated"] = bool(ml_simulated)

    total_duration = round(sum(c["out_sec"] - c["in_sec"] for c in cuts), 3)
    evidence_coverage = round(
        sum(1 for c in cuts if c.get("evidence")) / max(1, len(cuts)), 4
    )
    verified_cuts = sum(
        1 for c in cuts if all(e.get("timecode_verified") for e in c["evidence"])
    )

    edl_doc: dict[str, Any] = {
        "schema": "comfy_studio.edl.v1",
        "kind": MONTAGE_EDL_KIND,
        "title": title,
        "aspect_ratio": aspect_ratio,
        "total_duration_sec": total_duration,
        "cuts": cuts,
        "notes": (
            f"Montaje desde brief ({len(cuts)} cortes, {total_duration:.2f}s). "
            f"Términos: {', '.join(terms) or '-'}. "
            f"Personas: {', '.join(persons) or '-'}."
        ),
        "montage": {
            "brief": cleaned_brief,
            "query_terms": terms,
            "persons": persons,
            "time_range_sec": list(time_range) if time_range else None,
            "target_duration_sec": target_duration_sec,
            "retrieval": {
                **transcript_report,
                **visual_report,
                **identity_report,
                "candidates": len(all_candidates),
                "merged_candidates": len(merged),
            },
            "selection": selection_report,
            "evidence_coverage": evidence_coverage,
            "cuts_timecode_verified": verified_cuts,
            "perception_stale": bool(perception_stale),
            "simulated": bool(ml_simulated),
            "simulation_notice": SIMULATED_ML_NOTICE if ml_simulated else None,
        },
    }
    saved = store.save_edl_version(project_id, edl=edl_doc)
    return {
        "status": "planned",
        "project_id": project_id,
        "edl_id": saved["id"],
        "version": saved["version"],
        "edl": saved["edl"],
        "brief": cleaned_brief,
        "query_terms": terms,
        "persons": persons,
        "time_range_sec": list(time_range) if time_range else None,
        "cuts": cuts,
        "total_duration_sec": total_duration,
        "evidence_coverage": evidence_coverage,
        "cuts_timecode_verified": verified_cuts,
        "retrieval": edl_doc["montage"]["retrieval"],
        "selection": selection_report,
        "perception_stale": bool(perception_stale),
        "simulated": bool(ml_simulated),
        "simulation_notice": SIMULATED_ML_NOTICE if ml_simulated else None,
    }
