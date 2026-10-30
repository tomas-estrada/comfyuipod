"""Adaptadores de servidor: conectan el esqueleto de orquestación a los motores reales.

El grafo (fases 13–16) se escribió **stub-first + inyectable**: cada nodo recibe un
colaborador (`PlannerLLM`, `EngineRunner`, `ShotEvaluator`, `TemplateReselector`,
`Assembler`) y por defecto usa un stub determinista, de modo que CI corre sin GPU,
sin red y sin LLM. Este módulo provee las implementaciones **reales** de esos
colaboradores, envolviendo las piezas que ya viven en el estudio:

  - `LLMPlannerAdapter`   → el cliente LLM async del estudio (`agent/llm.py`).
  - `EngineRunnerAdapter` → `Engine.run(EngineRunSpec)` de los adaptadores de motor.
  - `QaShotEvaluator`     → el motor `qa` (FFmpeg nivel 1 + VBench nivel 2).
  - `reselector`          → `make_template_reselector` (Bloque 3, ya existente).
  - `EngineAssembler`     → editor (Remotion) + compositor (Natron) para el reel.

Criterio (consistente con todo el bloque): este módulo es el ÚNICO punto que toca
el mundo async/GPU; el grafo sigue siendo síncrono y puro. Todo adaptador:
  - nunca lanza hacia el grafo por un fallo del motor: devuelve la forma de dict
    tipada que el nodo espera (`{'ok': bool, ...}`), igual que los stubs;
  - respeta el cost cap del shot (lo pasa a `EngineRunSpec.cost_cap`);
  - funciona con motores cuyo `run` es síncrono (audio, qa, editor, compositor,
    ...) o async (comfyui, blender), detectando la corrutina.

`build_server_collaborators(...)` arma un `Collaborators` listo para el
`ProductionService` del servidor. Nada aquí se activa en CI salvo que se inyecte
explícitamente; el default del servicio sigue siendo stubs.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from typing import Any

from comfy_studio.engines import EngineRunSpec
from comfy_studio.orchestration.conductor import Collaborators
from comfy_studio.orchestration.evaluator import make_template_reselector
from comfy_studio.orchestration.planner import replan_shot_focalized
from comfy_studio.orchestration.state import ProductionState, Shot

logger = logging.getLogger("comfy_studio.orchestration.adapters")


# ---------------------------------------------------------------------------
# Helper: correr un `run` que puede ser síncrono o una corrutina (comfyui/blender)
# ---------------------------------------------------------------------------


def _run_engine_sync(engine: Any, spec: EngineRunSpec) -> Any:
    """Llama `engine.run(spec)` tolerando que sea síncrono o async.

    El `EngineRunner` del grafo es síncrono; algunos motores (ComfyUI, Blender)
    exponen `run` async. Si el resultado es una corrutina, la ejecuta en un bucle
    propio. Nunca debe llamarse desde dentro de un bucle de eventos vivo: el grafo
    corre en un hilo síncrono (job de fondo), así que esto es seguro.
    """
    result = engine.run(spec)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Normaliza un `EngineResult` (o dict) a la forma que el grafo espera."""
    if hasattr(result, "to_public_dict"):
        return result.to_public_dict()
    if isinstance(result, dict):
        return result
    return {"ok": False, "error": f"resultado de motor no reconocido: {type(result)!r}"}


# ---------------------------------------------------------------------------
# 1) PlannerLLM real → cliente LLM async del estudio
# ---------------------------------------------------------------------------


class LLMPlannerAdapter:
    """`PlannerLLM` respaldado por el cliente LLM async del estudio.

    El planner del grafo es síncrono y espera `plan(idea, context) -> dict` con
    la forma `{'sequences': [{'title', 'shots': [...]}]}`. Este adaptador arma el
    prompt, llama al cliente async (vía `asyncio.run`) y parsea el JSON. Si el
    modelo no devuelve JSON usable, cae a un plan mínimo de una secuencia / un
    shot en vez de lanzar, para que el grafo siga siendo robusto.
    """

    _SYSTEM = (
        "Eres un director técnico de un estudio de reels cortos. Descompón la idea "
        "en secuencias y shots de 3 a 7 segundos. Responde SOLO con JSON válido con "
        "la forma: {\"sequences\":[{\"title\":str,\"shots\":[{\"description\":str,"
        "\"duration_s\":number,\"depends_on\":[shot_id],\"layers\":[{\"kind\":str,"
        "\"engine\":str,\"params\":{}}]}]}]}. Motores válidos: comfyui, blender, "
        "heavy_video, upscaler, compositor. Sin texto fuera del JSON."
    )

    def __init__(self, llm_client: Any, *, timeout_s: float = 60.0) -> None:
        self._llm = llm_client
        self._timeout_s = timeout_s

    def plan(self, idea: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        user = idea
        if context:
            ctx_json = json.dumps(context, ensure_ascii=False)[:1000]
            user = f"{idea}\n\ncontexto: {ctx_json}"
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": user},
        ]
        try:
            response = asyncio.run(self._llm.complete(messages, temperature=0.3))
        except Exception as exc:  # noqa: BLE001 - nunca romper el grafo por el LLM
            logger.warning("LLMPlannerAdapter: el cliente LLM falló (%s); plan mínimo.", exc)
            return self._fallback_plan(idea)

        content = (getattr(response, "content", "") or "").strip()
        parsed = _extract_json(content)
        if not isinstance(parsed, dict) or not parsed.get("sequences"):
            logger.warning("LLMPlannerAdapter: respuesta sin 'sequences' usable; plan mínimo.")
            return self._fallback_plan(idea)
        return parsed

    @staticmethod
    def _fallback_plan(idea: str) -> dict[str, Any]:
        return {
            "sequences": [
                {
                    "title": "secuencia principal",
                    "shots": [
                        {
                            "description": idea[:200],
                            "layers": [
                                {
                                    "kind": "gen",
                                    "engine": "comfyui",
                                    "params": {"prompt": idea[:200]},
                                }
                            ],
                        }
                    ],
                }
            ]
        }


def _extract_json(text: str) -> Any:
    """Extrae el primer objeto JSON de una respuesta LLM (tolera fences/ruido)."""
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # quitar fence ```json ... ```
        cleaned = cleaned.split("```", 2)
        cleaned = cleaned[1] if len(cleaned) > 1 else text
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass
    # último recurso: el primer {...} balanceado.
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# 2) EngineRunner real → Engine.run de los adaptadores de motor
# ---------------------------------------------------------------------------


# motor que exige un grafo API de ComfyUI ya resuelto en el job.
_GRAPH_ENGINES: frozenset[str] = frozenset({"comfyui"})
# plantilla por defecto cuando la capa `gen` de comfyui no trae `template_id`
# ni `graph`: text-to-video (la forma de shot más común del planner).
_DEFAULT_COMFY_TEMPLATE = "wan_t2v"


class EngineRunnerAdapter:
    """`EngineRunner` que despacha cada capa al motor real correspondiente.

    Recibe un mapa `{nombre_motor: instancia}` (los adaptadores ya construidos en
    el servidor). `run_layer(engine, job, cost_cap)` arma un `EngineRunSpec`, llama
    al motor (tolerando async) y devuelve `{'ok', 'output_path'?, 'error'?}`. Un
    motor desconocido o un fallo del motor se devuelven como `ok=False` tipado,
    nunca como excepción hacia el grafo.

    Puente prompt→grafo (M6): el planner (real o stub) describe cada capa `gen`
    de comfyui con `params` (p.ej. `{"prompt": ...}`), no con un grafo API ya
    armado. `ComfyUIEngine.run` en cambio exige `job['graph']`. Cuando el job de
    un motor "de grafo" no trae `graph`, este adaptador lo resuelve desde el
    registro de plantillas: toma `template_id` (o `_DEFAULT_COMFY_TEMPLATE` para
    text-to-video) y renderiza el workflow inyectando los `params` de la capa.
    Así la generación real funciona aun sin un LLM que arme grafos a mano.
    """

    def __init__(
        self,
        engines: Mapping[str, Any],
        *,
        template_registry: Any | None = None,
        default_comfy_template: str = _DEFAULT_COMFY_TEMPLATE,
    ) -> None:
        self._engines = dict(engines)
        self._registry = template_registry
        self._default_template = default_comfy_template

    def _resolve_graph(self, job: dict[str, Any]) -> dict[str, Any]:
        """Devuelve `job` con `graph` resuelto desde una plantilla si falta.

        No lanza: si no hay registro o la plantilla falla, deja el job como está
        y el motor devolverá su error tipado de 'grafo vacío'.
        """
        if job.get("graph") or self._registry is None:
            return job
        template_id = job.get("template_id") or self._default_template
        # params del template = todo lo de la capa salvo claves de control.
        control_keys = {"kind", "shot", "template_id", "graph", "job_id", "project_id"}
        params = {k: v for k, v in job.items() if k not in control_keys}
        try:
            graph = self._registry.render_workflow(
                template_id,
                params,
                job_id=job.get("shot"),
                strict=False,
            )
        except Exception as exc:  # noqa: BLE001 - no romper el grafo; el motor reportará
            logger.warning(
                "EngineRunnerAdapter: no se pudo renderizar plantilla %r (%s).",
                template_id,
                exc,
            )
            return job
        resolved = dict(job)
        resolved["graph"] = graph
        resolved.setdefault("template_id", template_id)
        return resolved

    def run_layer(
        self, engine: str, job: dict[str, Any], *, cost_cap: float | None = None
    ) -> dict[str, Any]:
        impl = self._engines.get(engine)
        if impl is None:
            return {
                "ok": False,
                "engine": engine,
                "error": f"motor no disponible en el servidor: {engine!r}",
            }
        job = dict(job)
        if engine in _GRAPH_ENGINES:
            job = self._resolve_graph(job)
        spec = EngineRunSpec(job=job, cost_cap=cost_cap)
        try:
            raw = _run_engine_sync(impl, spec)
        except Exception as exc:  # noqa: BLE001 - tipificar, no romper el grafo
            logger.warning("EngineRunnerAdapter: motor %s lanzó (%s).", engine, exc)
            return {"ok": False, "engine": engine, "error": str(exc)[:500]}
        result = _result_to_dict(raw)
        # normalizar output_path desde el payload del motor si no está en la raíz.
        if "output_path" not in result:
            payload = result.get("payload")
            if isinstance(payload, dict):
                if payload.get("output_path"):
                    result["output_path"] = payload["output_path"]
                else:
                    # ComfyUI devuelve payload['outputs'] = [rutas]; usar la 1ª.
                    outputs = payload.get("outputs")
                    if isinstance(outputs, list) and outputs:
                        result["output_path"] = str(outputs[0])
        return result


# ---------------------------------------------------------------------------
# 3) ShotEvaluator real → motor `qa` (FFmpeg nivel 1 + VBench nivel 2)
# ---------------------------------------------------------------------------


class QaShotEvaluator:
    """`ShotEvaluator` respaldado por el motor `qa`.

    `evaluate(shot) -> {'vbench': float}` para que el evaluador del grafo lo
    compare contra `shot.acceptance['vbench_min']`. Llama al motor `qa` sobre la
    salida del shot (`shot.output_path`), mapea su resultado a un score numérico:
    si VBench (nivel 2) está presente usa su score; si no, deriva un score del
    veredicto FFmpeg nivel 1 (pass→alto, fallo→bajo). Nunca lanza.
    """

    def __init__(self, qa_engine: Any, *, pass_score: float = 0.8, fail_score: float = 0.2) -> None:
        self._qa = qa_engine
        self._pass_score = pass_score
        self._fail_score = fail_score

    def evaluate(self, shot: Shot) -> dict[str, float]:
        if not shot.output_path:
            return {"vbench": 0.0}
        job = {"video_path": shot.output_path, "acceptance": dict(shot.acceptance)}
        spec = EngineRunSpec(job=job, cost_cap=shot.cost_cap_usd or None)
        try:
            raw = _run_engine_sync(self._qa, spec)
        except Exception as exc:  # noqa: BLE001 - nunca romper el lazo de evaluación
            logger.warning("QaShotEvaluator: motor qa lanzó (%s).", exc)
            return {"vbench": 0.0}
        result = _result_to_dict(raw)
        if not result.get("ok", False):
            return {"vbench": self._fail_score}
        payload = result.get("payload") or {}
        vbench = payload.get("vbench")
        if isinstance(vbench, dict) and isinstance(vbench.get("score"), (int, float)):
            return {"vbench": float(vbench["score"])}
        # sin VBench: derivar del veredicto FFmpeg nivel 1.
        passed = bool(payload.get("passed", False))
        return {"vbench": self._pass_score if passed else self._fail_score}


# ---------------------------------------------------------------------------
# 5) Assembler real → editor (Remotion) + compositor (Natron)
# ---------------------------------------------------------------------------


class EngineAssembler:
    """`Assembler` respaldado por el motor editor (y compositor si aplica).

    `assemble(production_id, clips) -> {'ok', 'output_path'?}`. Pasa la línea de
    tiempo de clips aceptados al motor editor (Remotion) para producir el reel
    final. El compositor (Natron) es opcional y queda disponible para shots que
    requieran composición de capas antes del corte final (hoy el editor consume
    directamente los `output_path` de cada shot). Nunca lanza hacia el grafo.
    """

    def __init__(self, editor_engine: Any, *, compositor_engine: Any | None = None) -> None:
        self._editor = editor_engine
        self._compositor = compositor_engine

    def assemble(self, production_id: str, clips: list[dict[str, Any]]) -> dict[str, Any]:
        timeline = {
            "production_id": production_id,
            "clips": [
                {"path": c.get("path"), "duration_s": c.get("duration_s", 5.0)}
                for c in clips
            ],
        }
        spec = EngineRunSpec(job={"timeline": timeline, "mode": "assemble"})
        try:
            raw = _run_engine_sync(self._editor, spec)
        except Exception as exc:  # noqa: BLE001 - tipificar, no romper el grafo
            logger.warning("EngineAssembler: editor lanzó (%s).", exc)
            return {"ok": False, "error": str(exc)[:500]}
        result = _result_to_dict(raw)
        if not result.get("ok", False):
            return {"ok": False, "error": result.get("error", "fallo de ensamblaje")}
        output = result.get("output_path")
        if not output:
            payload = result.get("payload") or {}
            output = payload.get("output_path")
        return {"ok": True, "output_path": output, "clips": len(clips)}


# ---------------------------------------------------------------------------
# Armado del Collaborators del servidor
# ---------------------------------------------------------------------------


def build_server_collaborators(
    *,
    llm_client: Any | None = None,
    engines: Mapping[str, Any] | None = None,
    qa_engine: Any | None = None,
    editor_engine: Any | None = None,
    compositor_engine: Any | None = None,
    available_nodes: set[str] | None = None,
    template_registry: Any | None = None,
) -> Collaborators:
    """Arma el `Collaborators` real del servidor desde las piezas disponibles.

    Cada pieza es opcional: si no se provee, ese colaborador queda en `None` y el
    grafo usa su stub determinista para esa fase. Así el servidor puede cablear
    incrementalmente (p.ej. solo el runner y el qa) sin romper el resto.
    """
    planner = LLMPlannerAdapter(llm_client) if llm_client is not None else None
    runner = (
        EngineRunnerAdapter(engines, template_registry=template_registry)
        if engines
        else None
    )
    evaluator = QaShotEvaluator(qa_engine) if qa_engine is not None else None
    assembler = (
        EngineAssembler(editor_engine, compositor_engine=compositor_engine)
        if editor_engine is not None
        else None
    )
    reselect = make_template_reselector(available_nodes=available_nodes)

    def _replan(shot: Shot, state: ProductionState) -> bool:
        return replan_shot_focalized(shot, state, planner)

    return Collaborators(
        planner=planner,
        runner=runner,
        evaluator=evaluator,
        reselect_template=reselect,
        replan_shot=_replan if planner is not None else None,
        assembler=assembler,
    )
