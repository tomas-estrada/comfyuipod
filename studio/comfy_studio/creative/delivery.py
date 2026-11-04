"""Paquete de producción entregable (M7, Worker 7, sección 10).

`build_delivery_package` ensambla, a partir del brief activo y del estado del
proyecto, el paquete que el usuario recibe: video final + variantes, preview,
portada, subtítulos, título/caption/hashtags con justificación (y fuentes de
tendencias marcadas como no disponibles cuando no hay acceso a datos actuales:
nunca se inventan métricas), EDL editable, assets generados con procedencia y
las decisiones/limitaciones relevantes.

No publica nada en redes: preparar la pieza y publicarla son acciones distintas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .provenance import linked_transcriptions

DELIVERY_SCHEMA = "comfy_studio.delivery.v1"

#: cuántos hashtags como máximo por plataforma (sugerencia conservadora; no hay
#: regla universal — se etiqueta como hipótesis editorial, no dato de volumen).
_HASHTAGS_BY_PLATFORM = {
    "instagram": 5,
    "tiktok": 4,
    "youtube": 3,
    "x": 3,
    "facebook": 3,
    "linkedin": 3,
    "whatsapp": 0,
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _asset_urls(store: Any, project_id: str, asset_id: str | None) -> dict[str, str | None]:
    if not asset_id:
        return {"file_url": None, "download_url": None, "view_url": None}
    return {
        "file_url": f"/studio/api/projects/{project_id}/assets/{asset_id}/file",
        "download_url": f"/studio/api/projects/{project_id}/assets/{asset_id}/file?download=1",
        "view_url": f"/studio/api/view?project_id={project_id}&asset_id={asset_id}",
    }


def _title_caption_hashtags(brief: dict[str, Any]) -> dict[str, Any]:
    """Título/caption/hashtags derivados del contenido real del brief.

    Cada texto incluye su justificación y la referencia a los hechos
    (asset_facts) que lo respaldan. Las tendencias se marcan explícitamente
    como no disponibles: sin acceso a datos actuales no se inventa popularidad.
    """
    keywords = [k for k in (brief.get("keywords") or [])][:8]
    piece_type = brief.get("piece_type") or "other"
    platform = (brief.get("audience") or {}).get("platform")
    facts = brief.get("asset_facts") or []
    supporting = [f.get("id") for f in facts[:10]]

    title = (brief.get("main_message") or brief.get("prompt") or "Pieza del estudio")[:60].strip()
    caption_parts = [f"Pieza de tipo '{piece_type}'."]
    if brief.get("purpose"):
        caption_parts.append(str(brief["purpose"]))
    if facts:
        caption_parts.append(
            f"Respaldo: {len(facts)} hecho(s) del material con evidencia trazable."
        )
    caption = " ".join(caption_parts)[:500]

    max_tags = _HASHTAGS_BY_PLATFORM.get(platform or "", 3)
    hashtags = [f"#{k}" for k in keywords[:max_tags]] if max_tags else []
    if piece_type != "other":
        hashtags.append(f"#{piece_type}")

    return {
        "title": title,
        "caption": caption,
        "hashtags": hashtags,
        "justification": {
            "title": "Derivado del mensaje principal inferido del prompt; editable en el brief.",
            "caption": "Usa solo propósito del brief y hechos respaldados por asset_facts.",
            "hashtags": (
                "Derivados de las keywords del prompt; cantidad adaptada a la plataforma "
                "como hipótesis editorial (sin datos de volumen)."
            ),
        },
        "evidence_refs": supporting,
        "trend_data": {
            "available": False,
            "sources": [],
            "limitation": (
                "Sin acceso a datos actuales de tendencias/volumen/rankings en este momento; "
                "no se afirma popularidad ni probabilidad de viralidad. Si se habilita una "
                "fuente autorizada, cada tendencia debe conservar fuente, fecha, plataforma, "
                "región y justificación."
            ),
        },
    }


def build_delivery_package(
    store: Any,
    project_id: str,
    brief_row: dict[str, Any],
) -> dict[str, Any]:
    """Ensambla el paquete de entrega del último brief del proyecto."""
    project = store.get_project(project_id)
    if project is None:
        raise LookupError(f"Proyecto '{project_id}' no encontrado")
    brief = brief_row.get("brief") or {}

    # Video final: última EDL con output renderizado; variantes = todas las EDL con output.
    edls = store.list_edl_versions(project_id)
    rendered = [e for e in edls if e.get("output_asset_id")]
    final_edl = rendered[0] if rendered else (edls[0] if edls else None)
    final_asset_id = final_edl.get("output_asset_id") if final_edl else None

    variants = []
    for e in rendered:
        aid = e.get("output_asset_id")
        variants.append({
            "edl_id": e.get("id"),
            "edl_version": e.get("version"),
            "aspect_ratio": (e.get("edl") or {}).get("aspect_ratio"),
            "total_duration_sec": (e.get("edl") or {}).get("total_duration_sec"),
            "asset_id": aid,
            **_asset_urls(store, project_id, aid),
        })

    # Fallback: último asset de categoría outputs aunque no venga de EDL.
    if final_asset_id is None:
        outputs = store.list_assets(project_id, category="outputs")
        if outputs:
            final_asset_id = outputs[-1]["id"]

    # Preview: keyframes si hay, si no el propio final.
    keyframes = store.list_assets(project_id, category="keyframes")
    preview_ids = [k["id"] for k in keyframes[:8]] or ([final_asset_id] if final_asset_id else [])

    # Portada: primer keyframe verificable o primera imagen.
    cover_id = keyframes[0]["id"] if keyframes else None
    if cover_id is None:
        images = [a for a in store.list_assets(project_id) if a.get("kind") == "image"]
        cover_id = images[0]["id"] if images else None

    # Subtítulos: solo transcripciones exportables de assets usados por el EDL final.
    audio_plan = (brief.get("editorial") or {}).get("audio_plan") or {}
    subtitles_disabled = (
        any(
            isinstance(item, dict)
            and item.get("key") == "subtitles"
            and item.get("value") is False
            for item in (brief.get("user_constraints") or [])
        )
        or audio_plan.get("subtitles_policy") == "disabled"
    )
    trs = (
        [] if subtitles_disabled else linked_transcriptions(store, project_id, final_asset_id)
    )
    subtitles = None
    if trs:
        tid = trs[0]["id"]
        subtitles = {
            "transcription_id": tid,
            "source_asset_id": trs[0].get("asset_id"),
            "language": trs[0].get("language"),
            "diarized": bool(trs[0].get("diarized")),
            "srt_url": f"/studio/api/projects/{project_id}/transcriptions/{tid}/subtitles?fmt=srt",
            "vtt_url": f"/studio/api/projects/{project_id}/transcriptions/{tid}/subtitles?fmt=vtt",
        }

    # EDL editable (última versión, con o sin render).
    edl_summary = None
    if final_edl is not None:
        edl_doc = final_edl.get("edl") or {}
        edl_summary = {
            "edl_id": final_edl.get("id"),
            "version": final_edl.get("version"),
            "schema": edl_doc.get("schema"),
            "title": edl_doc.get("title"),
            "cuts_count": len(edl_doc.get("cuts") or []),
            "total_duration_sec": edl_doc.get("total_duration_sec"),
            "aspect_ratio": edl_doc.get("aspect_ratio"),
            "list_url": f"/studio/api/projects/{project_id}/edl",
            "render_url": (
                f"/studio/api/projects/{project_id}/edl/"
                f"{final_edl.get('version')}/render"
            ),
        }

    # Assets generados (salidas) con procedencia.
    outputs = store.list_assets(project_id, category="outputs")
    generated = [
        {
            "asset_id": a["id"],
            "filename": a.get("original_filename"),
            "created_at": a.get("created_at"),
            "provenance": a.get("metadata") or {},
            **_asset_urls(store, project_id, a["id"]),
        }
        for a in outputs
    ]

    evidence = brief_row.get("evidence") or {}
    simulated_facts = sum(1 for f in (brief.get("asset_facts") or []) if f.get("simulated"))
    limitations = list(evidence.get("limitations") or [])
    if simulated_facts:
        limitations.append(
            f"{simulated_facts} hecho(s) de asset_facts provienen de backends ML simulados "
            "(marcados simulated=true); no presentarlos como comprensión real del material."
        )
    limitations.extend(
        f"Pendiente: {p.get('question')}"
        for p in (brief.get("pending") or [])
        if isinstance(p, dict)
    )

    return {
        "schema": DELIVERY_SCHEMA,
        "project_id": project_id,
        "generated_at": _utc_now_iso(),
        "brief": {
            "id": brief_row.get("id"),
            "version": brief_row.get("version"),
            "status": brief_row.get("status"),
        },
        "video": {
            "asset_id": final_asset_id,
            **_asset_urls(store, project_id, final_asset_id),
        },
        "variants": variants,
        "preview_asset_ids": preview_ids,
        "cover": {"asset_id": cover_id, **_asset_urls(store, project_id, cover_id)},
        "subtitles": subtitles,
        "texts": _title_caption_hashtags(brief),
        "edl": edl_summary,
        "generated_assets": generated,
        "decisions": {
            "assumptions": brief.get("assumptions") or [],
            "user_constraints": brief.get("user_constraints") or [],
            "editorial_summary": {
                "promise": (brief.get("editorial") or {}).get("promise"),
                "structure_moments": len(
                    (brief.get("editorial") or {}).get("structure") or []
                ),
                "material_used": len(
                    ((brief.get("editorial") or {}).get("material") or {}).get("use") or []
                ),
                "material_skipped": len(
                    ((brief.get("editorial") or {}).get("material") or {}).get("skip") or []
                ),
            },
        },
        "limitations": limitations,
        "publishing": {
            "published": False,
            "note": (
                "Preparar la pieza y publicarla son acciones distintas; "
                "no se publica por defecto."
            ),
        },
    }
