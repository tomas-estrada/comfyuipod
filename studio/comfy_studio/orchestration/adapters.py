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
from pathlib import Path
from typing import Any

from comfy_studio.engines import EngineRunSpec
from comfy_studio.orchestration.conductor import Collaborators
from comfy_studio.orchestration.evaluator import make_template_reselector
from comfy_studio.orchestration.gpu_lease import GpuLease
from comfy_studio.orchestration.leased_runner import LeasedEngineRunner
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
        'la forma: {"sequences":[{"title":str,"shots":[{"description":str,'
        '"duration_s":number,"depends_on":[shot_id],"layers":[{"kind":str,'
        '"engine":str,"params":{}}]}]}]}. Motores válidos: comfyui, blender, '
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
        except Exception as exc:
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
        atlas_service: Any | None = None,
        comfyui_input_dir: str | None = None,
    ) -> None:
        self._engines = dict(engines)
        self._registry = template_registry
        self._default_template = default_comfy_template
        self._atlas = atlas_service
        self._comfyui_input_dir = comfyui_input_dir

    def _resolve_atlas_background(self, job: dict[str, Any]) -> dict[str, Any]:
        """Resuelve el fondo del atlas referenced en `job['atlas']` a un archivo local real.

        Valida que el ref exista en el catálogo, que la ruta quede dentro de
        files/ (sin path traversal) y que el archivo exista; inyecta
        `background_image` con la ruta absoluta para la plantilla. Si algo falla
        devuelve ok=False tipado (nunca un placeholder).
        """
        atlas_ctx = job.get("atlas")
        if not isinstance(atlas_ctx, dict) or self._atlas is None:
            return job
        try:
            available = self._atlas.available()
        except Exception:  # noqa: BLE001
            available = False
        if not available:
            return {
                "_atlas_error": "atlas no disponible en el pod (sin catálogo en STUDIO_ATLAS_DIR)"
            }
        ref_id = atlas_ctx.get("ref_id") or ""
        ref = self._atlas.store.get_reference(ref_id) if ref_id else None
        if ref is None:
            return {"_atlas_error": f"referencia de atlas desconocida: {ref_id!r}"}
        files_root = (self._atlas.root / "files").resolve()
        path = (files_root / ref.file_path).resolve()
        try:
            path.relative_to(files_root)
        except ValueError:
            return {"_atlas_error": "file_path del atlas escapa de files/ (path traversal)"}
        if not path.is_file():
            return {"_atlas_error": f"archivo del atlas ausente en disco: {ref.file_path}"}
        # integridad al (re)ejecutar: el hash debe corresponder al contenido elegido
        from comfy_studio.atlas.store import sha256_file

        if sha256_file(path) != ref.sha256:
            return {
                "_atlas_error": (
                    f"hash del fondo del atlas no coincide con el catálogo "
                    f"({ref.ref_id}); posible corrupción o cambio silencioso"
                )
            }
        resolved = dict(job)
        resolved["background_image"] = str(path)
        resolved["background_sha256"] = ref.sha256
        resolved["atlas_resolved"] = {
            "ref_id": ref.ref_id,
            "place_id": atlas_ctx.get("place_id"),
            "view_id": atlas_ctx.get("view_id"),
            "sha256": ref.sha256,
            "license": ref.license,
            "attribution": ref.attribution,
            "capture_date": ref.capture_date,
        }
        return resolved

    def _resolve_graph(self, job: dict[str, Any]) -> dict[str, Any]:
        """Devuelve `job` con `graph` resuelto desde una plantilla si falta.

        No lanza: si no hay registro o la plantilla falla, deja el job como está
        y el motor devolverá su error tipado de 'grafo vacío'.
        """
        if job.get("graph") or self._registry is None:
            return job
        template_id = job.get("template_id") or self._default_template
        # params del template = todo lo de la capa salvo claves de control.
        control_keys = {
            "kind",
            "shot",
            "template_id",
            "graph",
            "job_id",
            "project_id",
            "atlas",
            "background_image",
            "background_sha256",
            "atlas_resolved",
            "recipe_id",
            "recipe_unresolved",
            "lease_vram_mb",
            "lease_ram_mb",
        }
        params = {k: v for k, v in job.items() if k not in control_keys}
        # el fondo del atlas entra al grafo por la entrada de imagen declarada
        # (p. ej. wan_i2v -> image_input -> LoadImage). Si la plantilla no declara
        # entrada de imagen, el fondo NO llegaría: se marca para fallar en claro.
        if job.get("atlas_resolved") or job.get("background_image"):
            try:
                specs = self._registry.get_raw_definition(template_id)["parameters"]
            except Exception:  # noqa: BLE001
                specs = {}
            if "image_input" in specs:
                fname = Path(str(job["background_image"])).name
                params["image_input"] = fname
                if self._comfyui_input_dir:
                    import shutil as _shutil

                    dest_dir = Path(self._comfyui_input_dir)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / fname
                    if not dest.is_file():
                        _shutil.copy2(str(job["background_image"]), dest)
                job = dict(job)
                job["atlas_image_input"] = fname
                job["atlas_template_has_image"] = True
            else:
                job = dict(job)
                job["atlas_template_has_image"] = False
        try:
            graph = self._registry.render_workflow(
                template_id,
                params,
                job_id=job.get("shot"),
                strict=False,
            )
        except Exception as exc:
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
        resolved_atlas = self._resolve_atlas_background(job)
        if isinstance(resolved_atlas, dict) and "_atlas_error" in resolved_atlas:
            return {
                "ok": False,
                "engine": engine,
                "error": f"atlas: {resolved_atlas['_atlas_error']}",
            }
        if isinstance(resolved_atlas, dict):
            job = resolved_atlas
        if engine in _GRAPH_ENGINES:
            job = self._resolve_graph(job)
            if job.get("background_image") and job.get("atlas_template_has_image") is False:
                return {
                    "ok": False,
                    "engine": engine,
                    "error": (
                        "atlas: la plantilla seleccionada no declara entrada de imagen; "
                        "el fondo no llegaría al grafo (usar p. ej. wan_i2v)"
                    ),
                }
        spec = EngineRunSpec(job=job, cost_cap=cost_cap)
        try:
            raw = _run_engine_sync(impl, spec)
        except Exception as exc:
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
        except Exception as exc:
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
        # El `EditorEngine` espera el job en la forma de `TimelineSpec`:
        # `clips` en la RAÍZ (no anidado bajo `timeline`) con {path, duration_s,
        # transition_in?}, y un `output_path` obligatorio para el reel final.
        # Pasarle {"timeline": {...}} rompía con "timeline inválida: 'clips'".
        job = {
            "clips": [
                {
                    "path": c.get("path"),
                    "duration_s": c.get("duration_s", 5.0),
                    "transition_in": c.get("transition_in", "cut"),
                }
                for c in clips
            ],
            "output_path": f"projects/default/outputs/{production_id}_reel.mp4",
            "mode": "assemble",
        }
        spec = EngineRunSpec(job=job)
        try:
            raw = _run_engine_sync(self._editor, spec)
        except Exception as exc:
            logger.warning("EngineAssembler: editor lanzó (%s).", exc)
            return {"ok": False, "error": str(exc)[:500]}
        result = _result_to_dict(raw)
        if not result.get("ok", False):
            return {"ok": False, "error": result.get("error", "fallo de ensamblaje")}
        # el editor devuelve la ruta del reel en payload['output'] (o output_path).
        output = result.get("output_path")
        if not output:
            payload = result.get("payload") or {}
            output = payload.get("output") or payload.get("output_path")
        return {"ok": True, "output_path": output, "clips": len(clips)}


class _LeasedQaEngine:
    """Run QA through the same GPU arbitration as generation engines."""

    def __init__(self, engine: Any, lease: GpuLease, unload: Any | None) -> None:
        self._runner = LeasedEngineRunner(
            _SingleEngineRunner(engine),
            lease,
            owner="orchestration-qa",
            unload=unload,
        )

    def run(self, spec: EngineRunSpec) -> dict[str, Any]:
        return self._runner.run_layer("qa", spec.job, cost_cap=spec.cost_cap)


class _SingleEngineRunner:
    """Adapt one sync/async engine to the LeasedEngineRunner contract."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def run_layer(
        self, _name: str, job: dict[str, Any], *, cost_cap: float | None = None
    ) -> dict[str, Any]:
        raw = _run_engine_sync(self._engine, EngineRunSpec(job=job, cost_cap=cost_cap))
        return _result_to_dict(raw)


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
    atlas_service: Any | None = None,
    comfyui_input_dir: str | None = None,
    gpu_lease: GpuLease | None = None,
    unload_gpu: Any | None = None,
) -> Collaborators:
    """Arma el `Collaborators` real del servidor desde las piezas disponibles.

    Cada pieza es opcional: si no se provee, ese colaborador queda en `None` y el
    grafo usa su stub determinista para esa fase. Así el servidor puede cablear
    incrementalmente (p.ej. solo el runner y el qa) sin romper el resto.
    """
    planner = LLMPlannerAdapter(llm_client) if llm_client is not None else None
    runner = (
        EngineRunnerAdapter(
            engines,
            template_registry=template_registry,
            atlas_service=atlas_service,
            comfyui_input_dir=comfyui_input_dir,
        )
        if engines
        else None
    )
    if runner is not None and gpu_lease is not None:
        runner = LeasedEngineRunner(runner, gpu_lease, unload=unload_gpu)
    # Evaluador combinado: QA técnica del motor + QA de CONTENIDO del LLM de
    # visión. Con llm_client el shot debe pasar AMBAS (vbench_min y
    # semantic_min); sin llm_client queda la QA técnica sola.
    if qa_engine is not None and gpu_lease is not None:
        qa_engine = _LeasedQaEngine(qa_engine, gpu_lease, unload_gpu)
    evaluator = SemanticQaEvaluator(qa_engine, llm_client) if qa_engine is not None else None
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


# ---------------------------------------------------------------------------
# 6) Evaluador semántico: la QA técnica del motor + la QA de CONTENIDO del LLM
#    de visión. Cierra el lazo adaptativo de verdad: un shot puede ser
#    técnicamente limpio (VBench/FFmpeg) y aún así mostrar otra cosa que la
#    descrita por el plan. Con 'semantic' en el score, el escalón de replan se
#    dispara también por desajuste de contenido, no solo por frames negros.
# ---------------------------------------------------------------------------


def _extract_keyframes_sync(
    video_path: str, *, max_frames: int = 8, timeout_s: float = 60.0
) -> list[bytes]:
    """Extrae hasta `max_frames` keyframes JPEG de un vídeo con ffmpeg.

    Síncrono a propósito: el grafo corre en un hilo sin bucle de eventos.
    Falla blanda: sin ffmpeg o vídeo ilegible devuelve [] (el evaluador
    tratará la ausencia de frames como fallo tipado, no como crash).
    """
    import subprocess
    import tempfile
    from pathlib import Path

    from comfy_studio.media_bins import find_ffmpeg

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return []
    with tempfile.TemporaryDirectory(prefix="orch_semqa_") as tmp:
        out_dir = Path(tmp)
        cmd = [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-i",
            video_path,
            "-vf",
            f"thumbnail={max_frames}",
            "-frames:v",
            str(max_frames),
            str(out_dir / "kf_%02d.jpg"),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return []
        frames: list[bytes] = []
        for jf in sorted(out_dir.glob("kf_*.jpg")):
            frames.append(jf.read_bytes())
            if len(frames) >= max_frames:
                break
        return frames


class VisionShotEvaluator:
    """`ShotEvaluator` de CONTENIDO respaldado por el LLM de visión del estudio.

    Extrae keyframes del `output_path` del shot y pregunta al LLM de visión si
    muestran lo que la descripción del shot pide. Devuelve {'semantic': 0..1}.
    Fail-closed: sin vídeo, sin frames, sin LLM o sin JSON legible el score es
    0.0 — NUNCA se acepta un shot que no se pudo verificar semánticamente.
    """

    _SYSTEM = (
        "Eres el control de calidad de contenido de un estudio de vídeo. "
        "Se te muestran keyframes de un plano renderizado y la descripción "
        "que el director pidió. Juzga SOLO si el contenido visual cumple la "
        "descripción (acción, sujeto, escena). Responde EXCLUSIVAMENTE con "
        "JSON válido: {\"semantic\": <0.0..1.0>, \"reason\": \"<motivo breve>\"}. "
        "1.0 = cumple completamente; 0.0 = no corresponde."
    )

    def __init__(self, llm_client: Any, *, max_frames: int = 8, timeout_s: float = 90.0) -> None:
        self._llm = llm_client
        self._max_frames = max_frames
        self._timeout_s = timeout_s

    def evaluate(self, shot: Shot) -> dict[str, float]:
        from comfy_studio.agent.llm import build_vision_content_part

        if not shot.output_path:
            return {"semantic": 0.0}
        frames = _extract_keyframes_sync(
            shot.output_path, max_frames=self._max_frames
        )
        if not frames:
            return {"semantic": 0.0}
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Descripción pedida del plano: {shot.description}\n"
                    "¿Los keyframes muestran lo descrito? Responde SOLO el JSON."
                ),
            }
        ]
        for jpegb in frames:
            content.append(build_vision_content_part(jpegb, "image/jpeg"))
        messages = [
            {"role": "system", "content": self._SYSTEM},
            {"role": "user", "content": content},
        ]
        try:
            response = asyncio.run(
                asyncio.wait_for(
                    self._llm.complete(messages, temperature=0.1),
                    timeout=self._timeout_s,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("VisionShotEvaluator: LLM de visión falló (%s).", exc)
            return {"semantic": 0.0}
        score = _parse_semantic_json(getattr(response, "content", "") or "")
        return {"semantic": score}


def _parse_semantic_json(text: str) -> float:
    """Extrae {'semantic': x} del texto del LLM; falla a 0.0 (fail-closed)."""
    import re

    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            value = float(data.get("semantic", 0.0))
            return max(0.0, min(1.0, value))
        except (ValueError, TypeError):
            pass
    return 0.0


class SemanticQaEvaluator:
    """Combinación: métrica técnica (QaShotEvaluator) + semántica (visión).

    Devuelve {'vbench': <técnica>, 'semantic': <contenido>}; el
    `_passes_acceptance` del evaluador comprueba cada umbral `*_min` del
    acceptance del shot, de modo que un plan con {'vbench_min': 0.55,
    'semantic_min': 0.6} exige AMBAS dimensiones para aceptar. Sin LLM de
    visión (o con stub), semantic=0.0 falla cerrado — el shot replanifica o
    falla, nunca se acepta a ciegas.
    """

    def __init__(self, qa_engine: Any, llm_client: Any | None = None) -> None:
        self._tech = QaShotEvaluator(qa_engine)
        self._vision = VisionShotEvaluator(llm_client) if llm_client is not None else None

    def evaluate(self, shot: Shot) -> dict[str, float]:
        scores = dict(self._tech.evaluate(shot))
        if self._vision is not None:
            scores.update(self._vision.evaluate(shot))
        else:
            scores["semantic"] = 0.0
        return scores
