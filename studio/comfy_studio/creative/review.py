"""Revisión del video que REALMENTE se produjo (M7, Worker 7, sección 9).

`review_output` inspecciona el asset de salida renderizado (no el plan):

- mediciones TÉCNICAS reales vía ffprobe (duración, resolución, aspecto,
  audio, codecs) y comprobaciones objetivas contra el brief (duración dentro
  de tolerancia, aspect ratio, subtítulos disponibles cuando hay diálogo,
  integridad del archivo);
- evaluación EDITORIAL separada y honesta: lo que puede medirse se mide; lo
  que requiere juicio (apertura, ritmo, fidelidad al brief) se marca
  `requires: human_or_vlm` en vez de inventar una nota;
- correcciones LOCALIZADAS: cada issue señala el tramo/corte afectado y la
  acción concreta (recortar, re-renderizar en otro aspect, generar subtítulos),
  para no reconstruir toda la producción.

El informe se persiste en `project_meta` bajo `creative_review:{asset_id}`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from comfy_studio.perception.keyframes import probe_media_info

from .provenance import linked_transcriptions

REVIEW_SCHEMA = "comfy_studio.review.v1"

_ASPECT_TOLERANCE = 0.02


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _aspect_of(width: int, height: int) -> float:
    return round(width / height, 4) if height else 0.0


def _parse_aspect(aspect_ratio: str) -> float:
    try:
        w, h = aspect_ratio.split(":", 1)
        return round(float(w) / float(h), 4)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _constraint(brief: dict[str, Any], key: str) -> Any:
    for c in (brief.get("user_constraints") or []):
        if isinstance(c, dict) and c.get("key") == key:
            return c.get("value")
    return None


def _expects_audio(brief: dict[str, Any]) -> bool:
    """Resolve audio requirements without treating a negated music label as required."""
    audio_plan = (brief.get("editorial") or {}).get("audio_plan") or {}
    music_policy = audio_plan.get("music_policy")
    if music_policy not in ("required", "optional", "prohibited"):
        music_constraint = _constraint(brief, "music")
        if music_constraint is True:
            music_policy = "required"
        elif music_constraint is False:
            music_policy = "prohibited"
        else:
            music_text = str(audio_plan.get("music") or "").casefold()
            if any(phrase in music_text for phrase in ("sin música", "sin musica", "no music")):
                music_policy = "prohibited"
            elif any(
                phrase in music_text
                for phrase in ("música obligatoria", "musica obligatoria", "music required")
            ):
                music_policy = "required"
            else:
                # Legacy briefs often say music is permitted or only used when present.
                music_policy = "optional"

    dialogue_policy = audio_plan.get("dialogue_policy")
    legacy_dialogue = str(audio_plan.get("dialogue") or "").casefold()
    if dialogue_policy not in ("required", "preserve_if_present", "none"):
        dialogue_policy = (
            "preserve_if_present" if legacy_dialogue.startswith("conservar") else "none"
        )

    source_audio = audio_plan.get("has_audio_in_sources") is True
    legacy_transcribed_dialogue = legacy_dialogue.startswith("conservar")
    return (
        music_policy == "required"
        or dialogue_policy == "required"
        or (
            dialogue_policy == "preserve_if_present"
            and (source_audio or legacy_transcribed_dialogue)
        )
    )


def review_output(
    store: Any,
    project_id: str,
    brief: dict[str, Any],
    asset_id: str,
) -> dict[str, Any]:
    """Revisa un asset renderizado contra el brief. Devuelve el informe completo."""
    project = store.get_project(project_id)
    if project is None:
        raise LookupError(f"Proyecto '{project_id}' no encontrado")
    asset = store.get_asset(project_id, asset_id)
    if asset is None:
        raise LookupError(f"Asset '{asset_id}' no encontrado en el proyecto")
    path = store.get_asset_path(project_id, asset_id)

    issues: list[dict[str, Any]] = []
    technical: dict[str, Any] = {"probe_ok": False}

    if path is None or not path.is_file():
        issues.append({
            "severity": "error",
            "check": "existencia",
            "detail": "El archivo del asset no existe en disco",
            "timecode": None,
            "correction": "re-renderizar desde el EDL correspondiente",
        })
    else:
        probe = probe_media_info(path)
        technical = {
            "probe_ok": bool(probe.get("probe_ok")),
            "duration_sec": probe.get("duration_sec"),
            "width": probe.get("width"),
            "height": probe.get("height"),
            "aspect": _aspect_of(int(probe.get("width") or 0), int(probe.get("height") or 0)),
            "has_video": bool(probe.get("has_video")),
            "has_audio": bool(probe.get("has_audio")),
            "codec_name": probe.get("codec_name"),
            "audio_codec_name": probe.get("audio_codec_name"),
        }
        if not technical["probe_ok"]:
            issues.append({
                "severity": "error",
                "check": "integridad",
                "detail": "ffprobe no pudo verificar el archivo (probe_ok=false)",
                "timecode": None,
                "correction": "re-renderizar con ffmpeg y verificar de nuevo",
            })
        else:
            if not technical["has_video"]:
                issues.append({
                    "severity": "error",
                    "check": "video",
                    "detail": "El archivo no tiene stream de video",
                    "timecode": None,
                    "correction": "re-renderizar como video",
                })

            # duración vs restricción del usuario / objetivo del brief
            target = _constraint(brief, "duration_sec")
            if target is None:
                target = (brief.get("duration") or {}).get("target_sec")
            if target:
                dur = float(technical.get("duration_sec") or 0.0)
                tol = max(0.5, float(target) * 0.10)
                if abs(dur - float(target)) > tol:
                    issues.append({
                        "severity": "warning",
                        "check": "duración",
                        "detail": (
                            f"Duración medida {dur:.2f}s fuera de tolerancia "
                            f"de {target}s ±{tol:.2f}s"
                        ),
                        "timecode": [max(0.0, dur - tol), dur],
                        "correction": (
                            f"recortar el tramo final para acercarse a {target}s "
                            "(edición localizada del último corte del EDL)"
                            if dur > float(target)
                            else f"ampliar con material adicional hasta ~{target}s"
                        ),
                    })

            # aspect ratio vs restricción
            want_aspect = _constraint(brief, "aspect_ratio") or brief.get("aspect_ratio")
            if want_aspect:
                want = _parse_aspect(str(want_aspect))
                got = float(technical.get("aspect") or 0.0)
                if want and got and abs(got - want) > _ASPECT_TOLERANCE:
                    issues.append({
                        "severity": "error",
                        "check": "aspect_ratio",
                        "detail": f"Aspecto medido {got} no coincide con el pedido {want_aspect}",
                        "timecode": None,
                        "correction": f"re-renderizar el EDL en {want_aspect} (variante existente)",
                    })

            # La política estructurada evita falsos positivos por frases como "sin música".
            if _expects_audio(brief) and not technical["has_audio"]:
                issues.append({
                    "severity": "error",
                    "check": "audio",
                    "detail": (
                        "El brief espera audio (diálogo/música) pero el render "
                        "no tiene pista de audio"
                    ),
                    "timecode": None,
                    "correction": (
                        "re-renderizar conservando el audio original o añadiendo "
                        "la pista prevista"
                    ),
                })

    # subtítulos requeridos -> debe existir transcripción exportable
    audio_plan = (brief.get("editorial") or {}).get("audio_plan") or {}
    subtitles_policy = audio_plan.get("subtitles_policy")
    subs_required = _constraint(brief, "subtitles")
    if subs_required is None and subtitles_policy in ("required", "recommended", "disabled"):
        subs_required = subtitles_policy == "required"
    if subs_required is None:
        subs_required = audio_plan.get("subtitles") == "subtítulos obligatorios"
    subtitles_disabled = (
        _constraint(brief, "subtitles") is False or subtitles_policy == "disabled"
    )
    linked = (
        [] if subtitles_disabled else linked_transcriptions(store, project_id, asset_id)
    )
    subtitles_available = bool(linked)
    transcription_ref = linked[0].get("id") if linked else None
    if subs_required and not subtitles_available:
        issues.append({
            "severity": "error",
            "check": "subtítulos",
            "detail": (
                "El brief exige subtítulos pero no hay una transcripción exportable "
                "vinculada a los assets fuente de esta producción"
            ),
            "timecode": None,
            "correction": "transcribir un asset fuente incluido en el EDL y exportar SRT/VTT",
        })

    # Verificaciones editoriales que NO se pueden medir objetivamente aquí:
    # se declaran explícitamente como pendientes de humano/VLM, sin inventar notas.
    editorial_checks = [
        {"check": "apertura (hook)", "status": "pending", "requires": "human_or_vlm",
         "note": "El hook propuesto está en brief.editorial.hook con su evidencia."},
        {"check": "fidelidad al brief (promesa/mensaje)", "status": "pending",
         "requires": "human_or_vlm",
         "note": "Comparar contra brief.purpose y brief.main_message."},
        {"check": "ritmo y resolución", "status": "pending", "requires": "human_or_vlm",
         "note": "Ver estructura en brief.editorial.structure (momentos con asset+intervalo)."},
        {"check": "afirmaciones inventadas", "status": "pending",
         "requires": "human_or_vlm",
         "note": "Este endpoint no compara el contenido visual del render con brief.asset_facts."},
        {"check": "legibilidad de subtítulos en encuadre", "status": "pending",
         "requires": "human_or_vlm",
         "note": "SRT/VTT disponibles; la revisión visual del encuadre requiere ver el render."},
    ]
    music_constraint = _constraint(brief, "music")
    if music_constraint is not None or audio_plan.get("music_policy") in ("required", "prohibited"):
        editorial_checks.append({
            "check": "cumplimiento del contenido musical",
            "status": "pending",
            "requires": "human_or_vlm",
            "note": (
                "ffprobe confirma si existe una pista de audio, pero no distingue "
                "música añadida de diálogo/audio original."
            ),
        })

    error_count = sum(1 for i in issues if i.get("severity") == "error")
    warning_count = sum(1 for i in issues if i.get("severity") == "warning")

    return {
        "schema": REVIEW_SCHEMA,
        "project_id": project_id,
        "asset_id": asset_id,
        "asset_filename": asset.get("filename"),
        "brief_version_status": brief.get("status"),
        "reviewed_at": _utc_now_iso(),
        "technical": technical,
        "subtitles": {
            "required": bool(subs_required),
            "available": subtitles_available,
            "transcription_id": transcription_ref,
            "srt_url": (
                f"/studio/api/projects/{project_id}/transcriptions/{transcription_ref}/subtitles?fmt=srt"
                if transcription_ref else None
            ),
            "vtt_url": (
                f"/studio/api/projects/{project_id}/transcriptions/{transcription_ref}/subtitles?fmt=vtt"
                if transcription_ref else None
            ),
        },
        "issues": issues,
        "editorial_checks": editorial_checks,
        "summary": {
            "passed": error_count == 0,
            "errors": error_count,
            "warnings": warning_count,
            "measured": bool(technical.get("probe_ok")),
            "note": (
                "passed refleja SOLO mediciones técnicas objetivas; la aceptación editorial "
                "final requiere revisión humana o VLM (ver editorial_checks)."
            ),
        },
    }


def save_review(store: Any, project_id: str, asset_id: str, report: dict[str, Any]) -> None:
    """Persiste el informe de revisión en project_meta (reanudable tras reinicio)."""
    store.set_meta(project_id, f"creative_review:{asset_id}", report)


def latest_review(store: Any, project_id: str, asset_id: str) -> dict[str, Any] | None:
    value = store.get_meta(project_id, f"creative_review:{asset_id}")
    return value if isinstance(value, dict) else None
