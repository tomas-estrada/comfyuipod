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
from typing import Any, Protocol

from comfy_studio.engines import is_dynamic_enabled
from comfy_studio.templates import get_default_registry
from comfy_studio.templates.registry import TemplateRegistry


@dataclass(slots=True)
class DiscoveredTemplate:
    """Una plantilla candidata hallada en una fuente comunitaria (no curada).

    Forma mínima para poder buscar/cruzar igual que una curada: id, texto para
    casar la intención, nodos requeridos (para el cruce de capacidades) y la
    procedencia. El workflow JSON en sí se trae solo si el agente la elige.
    """

    template_id: str
    name: str
    category: str
    text: str  # resumen/descripción para el match léxico
    required_nodes: list[str] = field(default_factory=list)
    source: str = "community"


class DiscoveryProvider(Protocol):
    """Fuente de plantillas de la comunidad (ComfyUI-Manager, repos, etc.).

    Inyectable: el pod determinista no trae ninguno (búsqueda 100% local). El
    proveedor real se conecta en runtime y SOLO se consulta con STUDIO_DYNAMIC=1,
    respetando loopback y la regla n.º 6 (instalar solo vía Manager+PIP_CONSTRAINT).
    No lanza: devuelve [] si no puede buscar.
    """

    def discover(self, query: str, *, output_kind: str, limit: int) -> list[DiscoveredTemplate]:
        ...


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


@dataclass(slots=True)
class ChainStep:
    """Un paso de un sub-grafo: una plantilla elegida para una sub-intención.

    `feeds_from` es el índice del paso anterior cuya salida alimenta este paso
    (None para el primero). `viable` refleja si la plantilla del paso es usable.
    """

    order: int
    intent: str
    template_id: str | None
    params: dict[str, Any] = field(default_factory=dict)
    feeds_from: int | None = None
    viable: bool = False
    reason: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "intent": self.intent,
            "template_id": self.template_id,
            "feeds_from": self.feeds_from,
            "viable": self.viable,
            "reason": self.reason,
        }


@dataclass(slots=True)
class TemplateChain:
    """Sub-grafo de plantillas encadenadas para un shot complejo.

    `viable` es True solo si TODOS los pasos lo son: un eslabón roto invalida la
    cadena (el agente decide instalar el submotor que falta o replanificar).
    """

    steps: list[ChainStep] = field(default_factory=list)

    @property
    def viable(self) -> bool:
        return bool(self.steps) and all(s.viable for s in self.steps)

    def template_ids(self) -> list[str]:
        return [s.template_id for s in self.steps if s.template_id]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "viable": self.viable,
            "steps": [s.to_public_dict() for s in self.steps],
        }


_WORD_RE = re.compile(r"[a-záéíóúñ0-9]+", re.IGNORECASE)


def _tokenize(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text or "")}


class TemplateSelector:
    """Selecciona la plantilla más apta de la librería para un shot.

    Flujo (plan maestro Parte II):
      1. buscar candidatos por intención/estilo (léxico aquí; BM25s+semántico luego),
      2. cruzar con capacidades instaladas (required_nodes vs nodos disponibles),
      3. re-rankear los viables (FlashRank en fase posterior; aquí score léxico),
      4. adaptar el JSON (intent -> params reales de la plantilla),
      5. encadenar varias plantillas en un sub-grafo tipado,
      (b) descubrimiento dinámico: ampliar el índice con fuentes comunitarias,
          SOLO con STUDIO_DYNAMIC=1 y un proveedor inyectado.
    """

    def __init__(
        self,
        registry: TemplateRegistry | None = None,
        *,
        discovery_provider: DiscoveryProvider | None = None,
    ) -> None:
        self._registry = registry or get_default_registry()
        self._discovery = discovery_provider

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

        # Capa (b): descubrimiento dinámico. Solo con STUDIO_DYNAMIC=1 y un proveedor
        # inyectado; las plantillas comunitarias pasan por el MISMO cruce de capacidades
        # que las curadas (nada entra sin verificar nodos). Nunca rompe la búsqueda local.
        if self.discovery_enabled() and self._discovery is not None:
            try:
                found = self._discovery.discover(
                    intent.prompt_text(), output_kind=intent.output_kind, limit=top_k
                )
            except Exception:  # noqa: BLE001 - un proveedor externo no debe romper la selección
                found = []
            known = {c.template_id for c in candidates}
            for dt in found:
                if dt.template_id in known:
                    continue
                dscore = self._lexical_score(
                    query_tokens,
                    {
                        "name": dt.name,
                        "category": dt.category,
                        "summary": dt.text,
                        "description": "",
                    },
                )
                dmissing = (
                    [n for n in dt.required_nodes if n not in available_nodes]
                    if available_nodes is not None
                    else []
                )
                candidates.append(
                    TemplateCandidate(
                        template_id=dt.template_id,
                        name=dt.name,
                        category=dt.category or dt.source,
                        score=dscore,
                        viable=not dmissing,
                        required_nodes=list(dt.required_nodes),
                        missing_nodes=dmissing,
                        reason=(
                            "viable"
                            if not dmissing
                            else f"faltan nodos: {', '.join(dmissing)}"
                        ),
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

    # -- paso 5: encadenar plantillas en un sub-grafo ----------------------

    def chain(
        self,
        intents: list[ShotIntent],
        *,
        available_nodes: set[str] | None = None,
    ) -> TemplateChain:
        """Construir un sub-grafo de plantillas para un shot complejo.

        Cada intención es un paso (gen -> depth -> relight -> upscale). Para cada
        paso se selecciona la mejor plantilla, se adaptan sus params, y la salida
        del paso previo alimenta al siguiente (`feeds_from`). Un paso sin plantilla
        viable queda marcado no-viable con su motivo; la cadena entera es viable
        solo si todos los pasos lo son (un eslabón roto obliga a instalar el
        submotor que falta o a replanificar, no a fingir). Nunca lanza.
        """
        steps: list[ChainStep] = []
        for i, intent in enumerate(intents):
            # El mejor candidato viable; si no hay, el mejor candidato a secas para
            # reportar el motivo (p.ej. nodos que faltan).
            best = self.best(intent, available_nodes=available_nodes)
            if best is None:
                ranked = self.select(intent, available_nodes=available_nodes, top_k=1)
                fallback = ranked[0] if ranked else None
                steps.append(
                    ChainStep(
                        order=i,
                        intent=intent.intent,
                        template_id=fallback.template_id if fallback else None,
                        feeds_from=(i - 1) if i > 0 else None,
                        viable=False,
                        reason=(
                            fallback.reason
                            if fallback
                            else "ninguna plantilla casa con la intención"
                        ),
                    )
                )
                continue
            steps.append(
                ChainStep(
                    order=i,
                    intent=intent.intent,
                    template_id=best.template_id,
                    params=self.adapt(best.template_id, intent),
                    feeds_from=(i - 1) if i > 0 else None,
                    viable=True,
                    reason="viable",
                )
            )
        return TemplateChain(steps=steps)

    def chain_ids(
        self,
        intents: list[ShotIntent],
        *,
        available_nodes: set[str] | None = None,
    ) -> list[str]:
        """Atajo: solo los ids de plantilla viables de la cadena, en orden."""
        return self.chain(intents, available_nodes=available_nodes).template_ids()

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
