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
        """Adaptar modelos/resoluciones/params del workflow al shot.

        TODO (fase siguiente): mapear la intención a los `parameters` de la
        plantilla. Por ahora devuelve los DEFAULTS declarados por la plantilla
        (sin exigir los obligatorios ni adaptación creativa): un punto de partida
        que la fase de adaptación rellenará desde la intención.
        """
        raw = self._registry.get_raw_definition(template_id)
        specs: dict[str, Any] = raw.get("parameters", {})
        return {
            name: spec.get("default")
            for name, spec in specs.items()
            if "default" in spec
        }

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
