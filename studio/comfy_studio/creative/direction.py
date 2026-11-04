"""Dirección editorial (M7, Worker 7, sección 5).

`build_direction` traduce intención + evidencia en decisiones editoriales
visibles y editables: qué promesa hace la pieza, cómo abre, qué momentos la
estructuran, qué material se usa y cuál sobra (con motivo), cuándo usar
diálogo/música/voz/silencio/subtítulos y si corresponde un CTA.

Todo es heurístico y transparente (`method: "heuristic-v1"`): no impone una
estructura única de planos ni ritmos; el resultado queda en el brief para que
el usuario lo edite. Nada aquí inventa hechos: los momentos referencian
`asset_id` + intervalos de la evidencia.
"""

from __future__ import annotations

import re
from typing import Any

_METHOD = "heuristic-v1"

_PIECE_PROMISE = {
    "product": "Mostrar el producto en uso real y su beneficio principal en los primeros segundos.",
    "event": (
        "Contar el evento con sus datos exactos (qué, cuándo, dónde) "
        "y la energía del material."
    ),
    "interview": (
        "Dejar que la voz de la persona entrevistada lleve la historia; "
        "el montaje solo ordena."
    ),
    "story": "Construir arco narrativo con los momentos de mayor carga emocional del material.",
    "trend": "Entrar rápido al motivo de la tendencia usando el material más afín disponible.",
    "educational": "Explicar un punto concreto con claridad; un solo mensaje por pieza.",
    "ambient": "Crear atmósfera sin forzar narrativa; el material manda el ritmo.",
    "reel": "Abrir con el plano más fuerte disponible y sostener atención con cortes ágiles.",
    "other": "Cumplir el propósito declarado en el prompt con el material más relevante.",
}

_CTA_BY_PIECE = {
    "product": "Llamada a la acción orientada a la compra/contacto (definida por el usuario).",
    "event": (
        "Llamada a la acción con los datos exactos del evento "
        "(fecha/lugar/enlace del material)."
    ),
    "educational": "Invitación a guardar/compartir o seguir aprendiendo.",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9áéíóúñ]+", " ", (text or "").lower()).strip()


def _fact_asset_ids(facts: list[dict[str, Any]]) -> set[str]:
    return {f.get("asset_id") for f in facts if f.get("asset_id")}


def _select_material(
    intent: dict[str, Any],
    understanding: dict[str, Any],
) -> dict[str, Any]:
    """Selecciona qué assets entran y cuáles sobran, por intención y calidad.

    La cantidad de archivos no determina la cantidad a usar: se puntúa por
    afinidad con las keywords del prompt (nombre, etiquetas CLIP, transcripción)
    y se excluyen duplicados exactos (mismo sha256) e inválidos.
    """
    keywords = [k for k in intent.get("keywords", []) if len(k) >= 3]
    kw_set = set(keywords)
    assets = understanding.get("assets", [])
    facts = understanding.get("facts", [])

    # índice de afinidad por asset
    score: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}
    for asset in assets:
        aid = asset["asset_id"]
        s = 0.0
        why: list[str] = []
        name = _norm(f"{asset.get('original_filename') or ''} {asset.get('filename') or ''}")
        hits = [k for k in kw_set if k in name]
        if hits:
            s += 2.0 * len(hits)
            why.append(f"nombre contiene: {', '.join(hits[:5])}")
        for fact in facts:
            if fact.get("asset_id") != aid:
                continue
            claim = _norm(str(fact.get("claim") or ""))
            if fact.get("kind") == "clip_shot":
                claim += " " + _norm(str((fact.get("evidence") or {}).get("caption") or ""))
                hits = [k for k in kw_set if k in claim]
                if hits:
                    s += 1.5 * len(hits)
                    why.append(f"plano afín a: {', '.join(hits[:5])}")
            elif fact.get("kind") == "transcript_segment":
                hits = [k for k in kw_set if k in claim]
                if hits:
                    s += 2.5 * len(hits)
                    why.append(f"diálogo afín a: {', '.join(hits[:5])}")
        score[aid] = s
        reasons[aid] = why

    use: list[dict[str, Any]] = []
    skip: list[dict[str, Any]] = []
    seen_hash: set[str] = set()
    for asset in assets:
        aid = asset["asset_id"]
        if asset.get("status") == "missing_file":
            skip.append({
                "asset_id": aid,
                "reason": "archivo físico ausente; se excluye sin romper el proyecto",
            })
            continue
        sha = asset.get("sha256")
        if sha and sha in seen_hash:
            skip.append({
                "asset_id": aid,
                "reason": "duplicado exacto (sha256) de otro asset ya seleccionado",
            })
            continue
        if sha:
            seen_hash.add(sha)
        s = score.get(aid, 0.0)
        why = list(reasons.get(aid, []))
        if asset.get("status") == "probe_unverified":
            s -= 1.0
            why.append("sin verificación ffprobe (probe_ok=false)")
        if s > 0 or not keywords:
            use.append({
                "asset_id": aid,
                "reason": "; ".join(why) or "material general del proyecto",
                "evidence_refs": [
                    f.get("id") for f in facts if f.get("asset_id") == aid
                ][:20],
                "relevance_score": round(s, 3),
            })
        else:
            skip.append({
                "asset_id": aid,
                "reason": "sin afinidad con la intención del prompt; sobra para esta pieza",
            })
    use.sort(key=lambda u: u.get("relevance_score", 0.0), reverse=True)
    return {"use": use, "skip": skip, "method": _METHOD}


def _build_structure(
    intent: dict[str, Any],
    understanding: dict[str, Any],
) -> list[dict[str, Any]]:
    """Momentos de la pieza referenciados a evidencia real (asset + intervalo)."""
    facts = understanding.get("facts", [])
    piece_type = intent.get("piece_type") or "other"
    structure: list[dict[str, Any]] = []

    transcript_facts = [f for f in facts if f.get("kind") == "transcript_segment"]
    shot_facts = [f for f in facts if f.get("kind") == "clip_shot"]

    if piece_type in ("interview", "event") and transcript_facts:
        # La historia la cuentan los segmentos con más afinidad; el orden temporal se conserva.
        kw = set(intent.get("keywords", []))
        ranked = sorted(
            transcript_facts,
            key=lambda f: (
                -sum(1 for k in kw if k in _norm(str(f.get("claim") or ""))),
                (f.get("interval_sec") or [0])[0],
            ),
        )
        for moment, fact in enumerate(ranked[:6], start=1):
            structure.append({
                "moment": f"momento-{moment}",
                "description": f"Declaración del material: {str(fact.get('claim'))[:140]}",
                "asset_refs": [fact.get("asset_id")],
                "interval_sec": fact.get("interval_sec"),
                "evidence_refs": [fact.get("id")],
            })
        return structure

    if shot_facts:
        shots = sorted(shot_facts, key=lambda f: (f.get("interval_sec") or [0])[0])
        opening = shots[0]
        closing = shots[-1]
        structure.append({
            "moment": "apertura",
            "description": "Plano de apertura con el motivo principal del material.",
            "asset_refs": [opening.get("asset_id")],
            "interval_sec": opening.get("interval_sec"),
            "evidence_refs": [opening.get("id")],
        })
        middle = shots[len(shots) // 2] if len(shots) > 2 else None
        if middle is not None and middle is not opening:
            structure.append({
                "moment": "momento principal",
                "description": "Plano central del material seleccionado.",
                "asset_refs": [middle.get("asset_id")],
                "interval_sec": middle.get("interval_sec"),
                "evidence_refs": [middle.get("id")],
            })
        if closing is not opening:
            structure.append({
                "moment": "cierre",
                "description": "Plano de cierre para resolver la pieza.",
                "asset_refs": [closing.get("asset_id")],
                "interval_sec": closing.get("interval_sec"),
                "evidence_refs": [closing.get("id")],
            })
        return structure

    # Sin transcripción ni índice visual: estructura mínima sobre assets con archivo.
    assets = [
        a for a in understanding.get("assets", []) if a.get("status") != "missing_file"
    ]
    for moment, asset in enumerate(assets[:3], start=1):
        structure.append({
            "moment": f"momento-{moment}",
            "description": f"Material {asset.get('original_filename') or asset.get('asset_id')}",
            "asset_refs": [asset.get("asset_id")],
            "interval_sec": None,
            "evidence_refs": [],
        })
    return structure


def build_direction(
    intent: dict[str, Any],
    understanding: dict[str, Any],
) -> dict[str, Any]:
    """Dirección editorial completa, trazable a la evidencia del proyecto."""
    piece_type = intent.get("piece_type") or "other"
    facts = understanding.get("facts", [])
    has_speech = any(f.get("kind") == "transcript_segment" for f in facts)
    has_audio = any(
        (a.get("probe") or {}).get("has_audio")
        for a in understanding.get("assets", [])
        if (a.get("probe") or {}).get("probe_ok")
    )

    material = _select_material(intent, understanding)
    structure = _build_structure(intent, understanding)

    music = intent.get("music")
    subtitles = intent.get("subtitles")
    if subtitles is None:
        subtitles = has_speech
    cta = bool(intent.get("cta"))

    audio_plan = {
        "dialogue": (
            "conservar la voz original del material (hay transcripción)"
            if has_speech
            else "sin diálogo disponible en el material"
        ),
        "dialogue_policy": "preserve_if_present" if has_speech or has_audio else "none",
        "music": (
            "sin música añadida (restricción del usuario)" if music is False
            else "música incidental permitida según el tono"
            if music is True
            else "priorizar audio original; música solo si el material la trae"
        ),
        "music_policy": (
            "prohibited" if music is False else "required" if music is True else "optional"
        ),
        "voice_over": "no prevista salvo que el usuario la pida",
        "silence": (
            "permitida en piezas de ambiente"
            if piece_type == "ambient"
            else "solo pausas del montaje"
        ),
        "subtitles": "subtítulos obligatorios" if subtitles else "sin subtítulos",
        "subtitles_policy": (
            "required"
            if subtitles and intent.get("subtitles") is True
            else "recommended"
            if subtitles
            else "disabled"
        ),
        "has_audio_in_sources": has_audio,
    }

    hook_fact = next(
        (f for f in facts if f.get("kind") in ("transcript_segment", "clip_shot")),
        None,
    )
    hook = {
        "description": (
            "Abrir con el primer momento relevante del material "
            + (
                f"({hook_fact.get('asset_id')} @ {hook_fact.get('interval_sec')})"
                if hook_fact
                else ""
            )
        ),
        "evidence_refs": [hook_fact.get("id")] if hook_fact else [],
        "method": _METHOD,
    }

    return {
        "method": _METHOD,
        "promise": _PIECE_PROMISE.get(piece_type, _PIECE_PROMISE["other"]),
        "concept": (
            f"Pieza de tipo '{piece_type}' con tono '{intent.get('tone') or 'neutro'}' "
            f"para {intent.get('platform') or 'plataforma no especificada'}."
        ),
        "emotion": intent.get("tone") or "neutro",
        "hook": hook,
        "structure": structure,
        "material": material,
        "audio_plan": audio_plan,
        "cta": _CTA_BY_PIECE.get(piece_type) if cta else None,
        "consistency_refs": sorted(_fact_asset_ids(facts)),
    }
