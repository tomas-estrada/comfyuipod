"""Brief creativo persistente y editable (M7, Worker 7, secciones 4 y 5).

Contrato `comfy_studio.brief.v1`: un dict JSON que conserva propósito, tipo de
pieza, audiencia (hipótesis explícita), plataforma/idioma/región/duración,
concepto/tono/emoción/referencias, restricciones explícitas del usuario, hechos
respaldados por assets (con evidencia), decisiones asumidas, información
pendiente, presupuesto y criterios de aceptación, más la dirección editorial.

El parseo del prompt es DETERMINISTA (sin LLM): extrae restricciones explícitas
(duración, formato, plataforma, idioma, música, subtítulos, tono, tipo de pieza)
y deja constancia de cada una como `user_constraints` con el fragmento original.
Las inferencias del sistema viven en `assumptions` y nunca pisan las decisiones
del usuario (`user_constraints` prevalecen).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

BRIEF_SCHEMA = "comfy_studio.brief.v1"

PIECE_TYPES = (
    "product",
    "event",
    "interview",
    "story",
    "trend",
    "educational",
    "ambient",
    "reel",
    "other",
)

BRIEF_STATUSES = ("draft", "proposed", "approved", "rejected")

#: campos de nivel superior que el usuario puede editar vía PATCH (el resto se
#: recalcula o es inmutable por versión).
EDITABLE_FIELDS = (
    "purpose",
    "main_message",
    "piece_type",
    "audience",
    "duration",
    "aspect_ratio",
    "concept",
    "tone",
    "emotion",
    "style_refs",
    "user_constraints",
    "assumptions",
    "pending",
    "budget",
    "acceptance_criteria",
    "editorial",
    "status",
)

_ASPECTS = ("9:16", "16:9", "1:1", "4:3", "21:9", "3:2")

_DURATION_PATTERNS = (
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:seg(?:undos?)?|s\b|sec(?:onds?)?)", re.I), 1.0),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*min(?:utos?|s)?\b", re.I), 60.0),
    (re.compile(r"medio\s*minuto", re.I), 30.0),
    (re.compile(r"un\s*minuto\b|1\s*minuto\b", re.I), 60.0),
)

_PLATFORM_MAP = {
    "instagram": "instagram",
    "tiktok": "tiktok",
    "youtube": "youtube",
    "twitter": "x",
    "x ": "x",
    "facebook": "facebook",
    "linkedin": "linkedin",
    "whatsapp": "whatsapp",
    "reels": "instagram",
    "shorts": "youtube",
}

_ASPECT_HINTS = (
    ("vertical", "9:16"),
    ("reel", "9:16"),
    ("tiktok", "9:16"),
    ("stories", "9:16"),
    ("horizontal", "16:9"),
    ("youtube", "16:9"),
    ("cuadrado", "1:1"),
    ("square", "1:1"),
)

_LANGUAGE_HINTS = (
    (re.compile(r"\b(ingl[eé]s|english)\b", re.I), "en"),
    (re.compile(r"\b(espa[ñn]ol|spanish|castellano)\b", re.I), "es"),
    (re.compile(r"\b(portugu[eê]s|portuguese)\b", re.I), "pt"),
    (re.compile(r"\b(franc[eé]s|french)\b", re.I), "fr"),
)

_MUSIC_OFF = re.compile(
    r"sin\s+m[úu]sica|no\s+music|without\s+music|sin\s+sonido|no\s+sound|"
    r"silencio\s+total|mute|mudo\b",
    re.I,
)
_MUSIC_ON = re.compile(
    r"con\s+m[úu]sica|with\s+music|music\b"
    r"|m[úu]sica\s+(de\s+fondo|incidental|ambiente)",
    re.I,
)
_SUBS_OFF = re.compile(r"sin\s+subt[íi]tulos|no\s+subtitles|sin\s+captions", re.I)
_SUBS_ON = re.compile(r"subt[íi]tulos|subtitles|captions", re.I)
_TEXT_OFF = re.compile(r"sin\s+texto(\s+en\s+pantalla)?\b|no\s+text\s+on\s+screen", re.I)
_TEXT_ON = re.compile(
    r"texto\s+en\s+pantalla|lower\s+thirds?|text\s+on\s+screen"
    r"|gr[áa]ficos?\s+de\s+texto",
    re.I,
)
_CTA_ON = re.compile(r"llamada\s+a\s+la\s+acci[óo]n|call\s+to\s+action|\bcta\b", re.I)

_TONE_MAP = (
    ("brutal", "impactante"),
    ("impactante", "impactante"),
    ("emocion", "emocional"),
    ("emotivo", "emocional"),
    ("corporativo", "corporativo"),
    ("profesional", "corporativo"),
    ("divertido", "divertido"),
    ("[ée]pico", "épico"),
    ("[ií]ntimo", "íntimo"),
    ("minimalista", "minimalista"),
    ("oscuro", "oscuro"),
    ("luminoso", "luminoso"),
    ("alegre", "alegre"),
    ("urgente", "urgente"),
    ("n[óo]st[áa]lgico", "nostálgico"),
    ("futurista", "futurista"),
    ("c[áa]lido", "cálido"),
    ("frío", "frío"),
)

_PIECE_HINTS = (
    (re.compile(r"\b(reel|reels)\b", re.I), "reel"),
    (re.compile(r"entrevista|interview", re.I), "interview"),
    (re.compile(r"evento|event\b|concierto|conferencia|lanzamiento", re.I), "event"),
    (re.compile(r"producto|product\b|anuncio|publicidad|ads?\b|spot", re.I), "product"),
    (re.compile(r"tendencia|trend|viral", re.I), "trend"),
    (re.compile(r"educativo|tutorial|explic[aá]|c[óo]mo\s+hacer|aprende", re.I), "educational"),
    (re.compile(r"historia|narrativa|story\b|documental", re.I), "story"),
    (re.compile(r"ambiente|ambient|mood|b-roll|de ambiente", re.I), "ambient"),
)

_STOPWORDS = {
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas", "y", "o", "en", "con",
    "para", "por", "sin", "que", "a", "al", "del", "es", "son", "se", "su", "sus",
    "hazme", "hacer", "quiero", "video", "clip", "pieza", "favor",
    "the", "an", "and", "or", "of", "to", "in", "with", "for", "no",
    "make", "me", "please",
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _keywords(prompt: str, max_keywords: int = 12) -> list[str]:
    tokens = re.findall(r"[a-záéíóúñ0-9]+", (prompt or "").lower())
    seen: list[str] = []
    for tok in tokens:
        if tok in _STOPWORDS or len(tok) < 3 or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= max_keywords:
            break
    return seen


def _add_constraint(
    constraints: list[dict[str, Any]],
    key: str,
    value: Any,
    match: re.Match | None,
) -> None:
    constraints.append({
        "key": key,
        "value": value,
        "text": (match.group(0) if match else "").strip(),
        "source": "user",
    })


def parse_prompt(prompt: str) -> dict[str, Any]:
    """Extrae intención y restricciones EXPLÍCITAS del prompt (determinista).

    Devuelve un dict con `piece_type`, `constraints` (lista trazable al texto
    original), `tone`, `keywords`, `platform`, `aspect_ratio`, `language`,
    `duration_sec`, flags de música/subtítulos/texto/CTA y `audience_hint`.
    Solo lo que el usuario dijo explícitamente entra en `constraints`.
    """
    text = (prompt or "").strip()
    low = text.lower()

    constraints: list[dict[str, Any]] = []

    duration_sec: float | None = None
    for pattern, multiplier in _DURATION_PATTERNS:
        m = pattern.search(low)
        if m:
            if m.groups() and m.group(1):
                duration_sec = round(float(m.group(1).replace(",", ".")) * multiplier, 3)
                constraints.append(
                    {"key": "duration_sec", "value": duration_sec,
                     "text": m.group(0).strip(), "source": "user"}
                )
            else:
                duration_sec = float(multiplier)
                constraints.append(
                    {"key": "duration_sec", "value": duration_sec,
                     "text": m.group(0).strip(), "source": "user"}
                )
            break

    aspect_ratio: str | None = None
    for aspect in _ASPECTS:
        if aspect in low:
            aspect_ratio = aspect
            constraints.append(
                {"key": "aspect_ratio", "value": aspect, "text": aspect, "source": "user"}
            )
            break
    if aspect_ratio is None:
        for hint, aspect in _ASPECT_HINTS:
            if re.search(rf"\b{re.escape(hint)}\b", low):
                aspect_ratio = aspect
                constraints.append(
                    {"key": "aspect_ratio", "value": aspect, "text": hint, "source": "user"}
                )
                break

    platform: str | None = None
    for key, plat in _PLATFORM_MAP.items():
        if re.search(rf"\b{re.escape(key.strip())}\b", low):
            platform = plat
            constraints.append(
                {"key": "platform", "value": plat, "text": key.strip(), "source": "user"}
            )
            break

    language: str | None = None
    for pattern, lang in _LANGUAGE_HINTS:
        m = pattern.search(text)
        if m:
            language = lang
            constraints.append(
                {"key": "language", "value": lang, "text": m.group(0).strip(), "source": "user"}
            )
            break

    music: bool | None = None
    if _MUSIC_OFF.search(low):
        music = False
        _add_constraint(constraints, "music", False, _MUSIC_OFF.search(low))
    elif _MUSIC_ON.search(low):
        music = True
        _add_constraint(constraints, "music", True, _MUSIC_ON.search(low))

    subtitles: bool | None = None
    if _SUBS_OFF.search(low):
        subtitles = False
        _add_constraint(constraints, "subtitles", False, _SUBS_OFF.search(low))
    elif _SUBS_ON.search(low):
        subtitles = True
        _add_constraint(constraints, "subtitles", True, _SUBS_ON.search(low))

    text_on_screen: bool | None = None
    if _TEXT_OFF.search(low):
        text_on_screen = False
        _add_constraint(constraints, "text_on_screen", False, _TEXT_OFF.search(low))
    elif _TEXT_ON.search(low):
        text_on_screen = True
        _add_constraint(constraints, "text_on_screen", True, _TEXT_ON.search(low))

    cta = bool(_CTA_ON.search(low))
    if cta:
        _add_constraint(constraints, "cta", True, _CTA_ON.search(low))

    piece_type = "other"
    for pattern, ptype in _PIECE_HINTS:
        if pattern.search(text):
            piece_type = ptype
            break

    tone: str | None = None
    for needle, mapped in _TONE_MAP:
        if re.search(needle, low):
            tone = mapped
            break

    audience_hint: str | None = None
    m = re.search(r"(?:para|dirigido\s+a|audiencia(?:\s+de)?)\s+([^.,;\n]{3,60})", text, re.I)
    if m:
        audience_hint = m.group(1).strip()

    return {
        "piece_type": piece_type,
        "constraints": constraints,
        "tone": tone,
        "keywords": _keywords(text),
        "platform": platform,
        "aspect_ratio": aspect_ratio,
        "language": language,
        "duration_sec": duration_sec,
        "music": music,
        "subtitles": subtitles,
        "text_on_screen": text_on_screen,
        "cta": cta,
        "audience_hint": audience_hint,
        "raw_prompt": text,
    }


def _constraint_value(intent: dict[str, Any], key: str) -> Any:
    for c in intent.get("constraints", []):
        if c.get("key") == key:
            return c.get("value")
    return None


def new_brief(
    *,
    project_id: str,
    prompt: str,
    intent: dict[str, Any],
    evidence: dict[str, Any],
    editorial: dict[str, Any],
    status: str = "draft",
) -> dict[str, Any]:
    """Construye el documento `comfy_studio.brief.v1` a partir del intent parseado,
    la evidencia multimodal y la dirección editorial ya calculadas."""
    constraints = intent.get("constraints", [])
    piece_type = intent.get("piece_type") or "other"
    if piece_type not in PIECE_TYPES:
        piece_type = "other"

    duration_sec = _constraint_value(intent, "duration_sec")
    aspect_ratio = _constraint_value(intent, "aspect_ratio") or "16:9"
    platform = _constraint_value(intent, "platform")
    language = _constraint_value(intent, "language") or "es"
    music = _constraint_value(intent, "music")
    subtitles = _constraint_value(intent, "subtitles")

    keywords = intent.get("keywords", [])
    tone = intent.get("tone") or "neutro"

    assumptions: list[dict[str, str]] = []
    if duration_sec is None:
        assumptions.append({
            "text": (
                "Duración no especificada; se propone la duración por defecto "
                "de la plataforma/pieza."
            ),
            "basis": "ausencia de restricción explícita en el prompt",
        })
    if aspect_ratio is None:
        assumptions.append({
            "text": "Formato no especificado; se asume 16:9 horizontal.",
            "basis": "ausencia de restricción explícita en el prompt",
        })
    if language is None or not _constraint_value(intent, "language"):
        assumptions.append({
            "text": "Idioma no especificado; se asume español (es).",
            "basis": "ausencia de restricción explícita en el prompt",
        })
    if platform is None:
        assumptions.append({
            "text": "Plataforma no especificada; se optimiza para feed vertical multiplataforma.",
            "basis": "ausencia de restricción explícita en el prompt",
        })
    if music is None:
        assumptions.append({
            "text": "Música no especificada; se prioriza el diálogo original cuando existe.",
            "basis": "presencia de transcripción en el material",
        })
    if subtitles is None:
        subtitles_default = any(
            f.get("kind") == "transcript_segment" for f in evidence.get("facts", [])
        )
        if subtitles_default:
            assumptions.append({
                "text": "Hay diálogo en el material; se incluyen subtítulos por defecto.",
                "basis": "segmentos de transcripción presentes en la evidencia",
            })
        else:
            assumptions.append({
                "text": "Sin diálogo detectado; no se exigen subtítulos.",
                "basis": "sin transcripciones en la evidencia",
            })
            subtitles_default = False
        subtitles = subtitles_default
    if _constraint_value(intent, "subtitles") is not None:
        subtitles = bool(_constraint_value(intent, "subtitles"))

    facts = evidence.get("facts", [])
    asset_facts = [
        {
            "id": f.get("id"),
            "kind": f.get("kind"),
            "asset_id": f.get("asset_id"),
            "interval_sec": f.get("interval_sec"),
            "method": f.get("method"),
            "simulated": f.get("simulated"),
            "evidence": f.get("evidence", {}),
            "claim": f.get("claim"),
        }
        for f in facts[:200]
    ]

    acceptance: list[dict[str, str]] = [
        {
            "id": "ac-no-invented-facts",
            "check": (
                "Toda afirmación en título/caption/subtítulos trazable a "
                "evidencia del proyecto"
            ),
            "target": "100% de afirmaciones con referencia de evidencia",
            "how_verified": "revisión del paquete de entrega contra `asset_facts`",
        },
        {
            "id": "ac-render-integrity",
            "check": "El render final es reproducible y verificable (ffprobe)",
            "target": "probe_ok=true, duración y formato coherentes con el brief",
            "how_verified": "review_output sobre el asset de salida",
        },
    ]
    if duration_sec is not None:
        acceptance.append({
            "id": "ac-duration",
            "check": "Duración del entregable dentro de tolerancia de la restricción del usuario",
            "target": f"{duration_sec}s ±10%",
            "how_verified": "ffprobe sobre el render final",
        })
    if _constraint_value(intent, "aspect_ratio") is not None:
        acceptance.append({
            "id": "ac-aspect",
            "check": "Relación de aspecto del entregable",
            "target": str(aspect_ratio),
            "how_verified": "ffprobe (width/height) sobre el render final",
        })
    if subtitles:
        acceptance.append({
            "id": "ac-subtitles",
            "check": "Subtítulos presentes, legibles y sincronizados cuando hay diálogo",
            "target": "SRT/VTT exportable desde la transcripción del proyecto",
            "how_verified": "endpoint de subtítulos + review del render",
        })
    if music is False:
        acceptance.append({
            "id": "ac-no-music",
            "check": "Sin pista de música añadida (solo audio original o silencio)",
            "target": "sin música generada",
            "how_verified": "revisión del plan de audio y del render",
        })
    if _constraint_value(intent, "language") is not None:
        acceptance.append({
            "id": "ac-language",
            "check": "Idioma de textos y voz coherente con la restricción",
            "target": str(language),
            "how_verified": "revisión editorial (humana o VLM)",
        })

    budget = {
        "cost_cap_usd": 0.0,
        "engines": ["editor", "qa"],
        "models": [],
        "llm_calls_estimate": 0,
        "notes": (
            "Montaje sobre assets existentes con ffmpeg (editor) + QA con ffprobe/ffmpeg; "
            "sin generación de video por defecto salvo que el brief lo exija. "
            "Presupuesto en USD pendiente de definir por el usuario (0 = sin tope explícito)."
        ),
    }

    audience = {
        "hypothesis": intent.get("audience_hint") or (
            f"Audiencia general en {platform or 'redes sociales'}; "
            "hipótesis a validar con datos reales."
        ),
        "platform": platform,
        "language": language,
        "region": None,
        "is_hypothesis": True,
    }

    return {
        "schema": BRIEF_SCHEMA,
        "project_id": project_id,
        "prompt": prompt,
        "status": status if status in BRIEF_STATUSES else "draft",
        "purpose": editorial.get("promise", ""),
        "main_message": " ".join(keywords[:6]) if keywords else (prompt or "")[:120],
        "piece_type": piece_type,
        "audience": audience,
        "duration": {
            "target_sec": duration_sec,
            "min_sec": round(duration_sec * 0.9, 3) if duration_sec else None,
            "max_sec": round(duration_sec * 1.1, 3) if duration_sec else None,
        },
        "aspect_ratio": aspect_ratio,
        "concept": editorial.get("concept", ""),
        "tone": tone,
        "emotion": editorial.get("emotion", tone),
        "style_refs": [],
        "keywords": keywords,
        "user_constraints": constraints,
        "asset_facts": asset_facts,
        "assumptions": assumptions,
        "pending": [],
        "budget": budget,
        "acceptance_criteria": acceptance,
        "editorial": editorial,
        "feedback": [],
        "created_at": _utc_now_iso(),
    }


def validate_brief(doc: dict[str, Any]) -> dict[str, Any]:
    """Valida la forma mínima del contrato; devuelve el doc si es válido."""
    if not isinstance(doc, dict):
        raise ValueError("El brief debe ser un objeto JSON")
    if doc.get("schema") != BRIEF_SCHEMA:
        raise ValueError(f"schema inválido: {doc.get('schema')!r} (esperado {BRIEF_SCHEMA})")
    if doc.get("piece_type") not in PIECE_TYPES:
        raise ValueError(f"piece_type inválido: {doc.get('piece_type')!r}")
    if doc.get("status") not in BRIEF_STATUSES:
        raise ValueError(f"status inválido: {doc.get('status')!r}")
    if not isinstance(doc.get("asset_facts"), list):
        raise ValueError("asset_facts debe ser una lista")
    for fact in doc["asset_facts"]:
        if not isinstance(fact, dict) or not fact.get("asset_id"):
            raise ValueError("Cada asset_fact requiere asset_id")
    if not isinstance(doc.get("user_constraints"), list):
        raise ValueError("user_constraints debe ser una lista")
    if not isinstance(doc.get("editorial"), dict):
        raise ValueError("editorial debe ser un objeto")
    if not isinstance(doc.get("acceptance_criteria"), list):
        raise ValueError("acceptance_criteria debe ser una lista")
    return doc


def build_brief(
    *,
    project_id: str,
    prompt: str,
    evidence: dict[str, Any],
    editorial: dict[str, Any],
    overrides: dict[str, Any] | None = None,
    status: str = "draft",
) -> dict[str, Any]:
    """Construye + valida un brief aplicando `overrides` del usuario (prevalecen)."""
    intent = evidence.get("intent") or parse_prompt(prompt)
    brief = new_brief(
        project_id=project_id,
        prompt=prompt,
        intent=intent,
        evidence=evidence,
        editorial=editorial,
        status=status,
    )
    for key, value in (overrides or {}).items():
        if key in EDITABLE_FIELDS:
            brief[key] = value
    if overrides and "status" in overrides and overrides["status"] in BRIEF_STATUSES:
        brief["status"] = overrides["status"]
    return validate_brief(brief)
