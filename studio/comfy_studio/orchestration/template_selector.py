"""Selector de plantillas — Eje 3 de la orquestación (M6 Bloque 3, esqueleto).

El agente no debe tener visión de túnel usando siempre los mismos flujos básicos
ni inventar grafos a ciegas. Este selector opera sobre la **librería amplia** de
plantillas: dado un shot fuera de lo común, BUSCA la plantilla más apta, la CRUZA
con las capacidades realmente instaladas del motor, la adapta y (fase posterior)
encadena varias cuando el shot lo exige.

Se construye SOBRE el `TemplateRegistry` ya existente (`comfy_studio/templates`):
cada plantilla curada ya trae `category`, `summary`, `description`, `models` y
`required_nodes`. No se inventa un formato nuevo.

Librería híbrida (decisión del plan maestro):
  - (a) índice curado versionado = el `TemplateRegistry` actual (siempre activo);
  - (b) descubrimiento dinámico de la comunidad = capa futura gobernada por
    `STUDIO_DYNAMIC` (fase posterior; aquí solo se deja el gancho).

Este archivo es el ESQUELETO: búsqueda léxica + cruce de capacidades + ranking.
La adaptación de JSON y el encadenado llegan en fases siguientes (señaladas TODO).
Todo tipado; ninguna función lanza por "no encontrado": devuelve resultados vacíos.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from comfy_studio.engines import is_dynamic_enabled
from comfy_studio.templates import get_default_registry
from comfy_studio.templates.registry import TemplateRegistry


@dataclass(slots=True)
class TemplateCandidate:
    """Una plantilla candidata para un shot, con su score y por qué es (in)viable."""

    template_id: str
    name: str
    category: str
    score: float
    viable: bool
    required_nodes: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    reason: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "category": self.category,
            "score": round(self.score, 4),
            "viable": self.viable,
            "required_nodes": list(self.required_nodes),
            "missing_nodes": list(self.missing_nodes),
            "reason": self.reason,
        }


@dataclass(slots=True)
class ShotIntent:
    """Lo que un shot necesita, en términos que el selector puede casar."""

    intent: str  # descripción libre: "coche derrapando al atardecer, cámara orbital"
    output_kind: str = ""  # "video" | "image" | "three_d" | "" (cualquiera)
    style_tags: list[str] = field(default_factory=list)
    duration_s: float | None = None  # si se da, se mapea a 'frames' (~16 fps)
    orientation: str = ""  # "" | "horizontal" | "vertical" (9:16)
    negative: str = ""  # elementos a suprimir (negative_prompt), si procede
    seed: int | None = None  # semilla determinista opcional

    def prompt_text(self) -> str:
        """La intención + los tags de estilo, como una descripción cinematográfica."""
        parts = [self.intent.strip()]
        if self.style_tags:
            parts.append(", ".join(t.strip() for t in self.style_tags if t.strip()))
        return ". ".join(p for p in parts if p)


_WORD_RE = re.compile(r"[a-záéíóúñ0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


class TemplateSelector:
    """Selecciona la plantilla más apta de la librería para un shot.

    Flujo (plan maestro Parte II):
      1. buscar candidatos por intención/estilo (léxico aquí; BM25s+semántico luego),
      2. cruzar con capacidades instaladas (required_nodes vs nodos disponibles),
      3. re-rankear los viables (FlashRank en fase posterior; aquí score léxico),
      4. adaptar el JSON (TODO fase siguiente),
      5. encadenar varias plantillas (TODO fase siguiente).
    """

    def __init__(self, registry: TemplateRegistry | None = None) -> None:
        self._registry = registry or get_default_registry()

    # -- paso 1+2+3: buscar, cruzar, rankear --------------------------------

    def select(
        self,
        intent: ShotIntent,
        *,
        available_nodes: set[str] | None = None,
        top_k: int = 5,
    ) -> list[TemplateCandidate]:
        """Devuelve candidatos ordenados por aptitud, marcando viabilidad.

        `available_nodes`: el conjunto de tipos de nodo que el ComfyUIEngine
        reporta instalados (`list()`/`/object_info`). Si es None, no se filtra por
        capacidad (todos se consideran potencialmente viables) — útil en diseño
        offline; en ejecución real SIEMPRE se pasa el conjunto real.
        """
        query_tokens = _tokenize(intent.intent) | {t.lower() for t in intent.style_tags}
        candidates: list[TemplateCandidate] = []

        for tpl in self._registry.list_templates(include_workflow=False):
            if intent.output_kind and tpl.get("output_kind") != intent.output_kind:
                continue
            score = self._lexical_score(query_tokens, tpl)
            required = list(tpl.get("required_nodes") or [])
            missing = (
                [n for n in required if n not in available_nodes]
                if available_nodes is not None
                else []
            )
            # Una plantilla no disponible por pesos (p.ej. 3D sin descargar) no es viable.
            weights_ok = bool(tpl.get("available", True))
            viable = weights_ok and not missing
            reason = self._reason(weights_ok, missing)
            candidates.append(
                TemplateCandidate(
                    template_id=tpl["id"],
                    name=tpl["name"],
                    category=tpl.get("category", ""),
                    score=score,
                    viable=viable,
                    required_nodes=required,
                    missing_nodes=missing,
                    reason=reason,
                )
            )

        # Viables primero, luego por score; los no viables al final (el agente puede
        # decidir instalar el submotor que falta si STUDIO_DYNAMIC lo permite).
        candidates.sort(key=lambda c: (c.viable, c.score), reverse=True)
        return candidates[:top_k]

    def best(
        self,
        intent: ShotIntent,
        *,
        available_nodes: set[str] | None = None,
    ) -> TemplateCandidate | None:
        """El mejor candidato VIABLE, o None si ninguno lo es."""
        for c in self.select(intent, available_nodes=available_nodes, top_k=20):
            if c.viable:
                return c
        return None

    # -- paso 4: adaptar el JSON (TODO fase siguiente) ----------------------

    def adapt(self, template_id: str, intent: ShotIntent) -> dict[str, Any]:
        """Mapear la intención del shot a los parámetros reales de la plantilla.

        Rellena los params prompt-like con la descripción del shot, infiere
        dimensiones de la orientación, mapea duración a frames (~16 fps), y deja
        los defaults de la plantilla para el resto. Parámetros que la plantilla no
        declara se ignoran (seguro). No valida aquí (eso lo hace render_workflow);
        devuelve un dict listo para `validate_params`/`render_workflow`.
        """
        raw = self._registry.get_raw_definition(template_id)
        specs: dict[str, Any] = raw.get("parameters", {})
        params: dict[str, Any] = {}

        for name, spec in specs.items():
            if "default" in spec:
                params[name] = spec.get("default")

        # 1. Prompt principal: el primer string requerido recibe la descripción.
        prompt_text = intent.prompt_text()
        for name, spec in specs.items():
            if spec.get("type") == "string" and spec.get("required") and prompt_text:
                params[name] = prompt_text
                break
        # Alias comunes aunque no sean required.
        if prompt_text and "prompt" in specs:
            params["prompt"] = prompt_text

        # 2. negative_prompt si el shot lo trae y la plantilla lo soporta.
        if intent.negative and "negative_prompt" in specs:
            params["negative_prompt"] = intent.negative

        # 3. Orientación -> width/height (vertical 9:16 intercambia los defaults).
        if intent.orientation == "vertical" and "width" in specs and "height" in specs:
            w = specs["width"].get("default")
            h = specs["height"].get("default")
            if isinstance(w, int) and isinstance(h, int) and w >= h:
                params["width"], params["height"] = h, w

        # 4. Duración -> frames (~16 fps), acotado por el rango de la plantilla.
        if intent.duration_s and "frames" in specs:
            fps = 16
            frames = max(1, round(intent.duration_s * fps))
            fmax = specs["frames"].get("max")
            fmin = specs["frames"].get("min")
            if isinstance(fmax, (int, float)):
                frames = min(frames, int(fmax))
            if isinstance(fmin, (int, float)):
                frames = max(frames, int(fmin))
            params["frames"] = frames

        # 5. Seed determinista opcional.
        if intent.seed is not None and "seed" in specs:
            params["seed"] = int(intent.seed)

        return params

    # -- paso 5: encadenar plantillas (TODO fase siguiente) -----------------

    def chain(self, intents: list[ShotIntent]) -> list[str]:
        """Encadenar varias plantillas para un shot complejo (gen->depth->relight->upscale).

        TODO (fase siguiente): construir el sub-grafo multi-plantilla. Por ahora
        devuelve la mejor plantilla por intención, sin dependencias entre ellas.
        """
        out: list[str] = []
        for intent in intents:
            best = self.best(intent)
            if best is not None:
                out.append(best.template_id)
        return out

    # -- gancho (b): descubrimiento dinámico (TODO, gobernado por STUDIO_DYNAMIC) --

    def discovery_enabled(self) -> bool:
        """¿Se permite ampliar el índice con fuentes comunitarias en runtime?"""
        return is_dynamic_enabled()

    # -- internos ------------------------------------------------------------

    @staticmethod
    def _lexical_score(query_tokens: set[str], tpl: dict[str, Any]) -> float:
        """Solapamiento léxico simple entre la intención y los metadatos de la plantilla.

        Fase siguiente: sustituir por BM25s (retrieve) + semántico y re-rank FlashRank.
        """
        if not query_tokens:
            return 0.0
        hay = " ".join(
            str(tpl.get(k, ""))
            for k in ("name", "category", "summary", "description")
        )
        hay_tokens = _tokenize(hay)
        if not hay_tokens:
            return 0.0
        overlap = len(query_tokens & hay_tokens)
        return overlap / len(query_tokens)

    @staticmethod
    def _reason(weights_ok: bool, missing: list[str]) -> str:
        if not weights_ok:
            return "pesos de la plantilla no disponibles en este pod"
        if missing:
            return f"faltan nodos: {', '.join(missing)}"
        return "viable"
