"""Loop principal del agente LLM de comfy_studio con tool-calling, memoria por proyecto y SSE."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..comfyui_client import ComfyUIClient
from ..config import RedactingLogFilter, SecretRedactor, StudioConfig
from ..progress import ProgressBroker
from ..projects import ProjectStore
from .llm import (
    OpenAICompatibleLLMClient,
    build_vision_content_part,
    validate_DataUrl_only,
    validate_no_auth_urls_in_messages,
)
from .tools import (
    ToolBelt,
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
    create_restricted_belt,
)

logger = logging.getLogger("comfy_studio.agent.loop")

DEFAULT_MAX_ITERATIONS = 12
DEFAULT_MAX_LLM_CALLS_PER_TURN = 24
HISTORY_SUMMARY_THRESHOLD = 40
HISTORY_KEEP_RECENT = 20
SUB_AGENT_MAX_ITERATIONS = 4

# M1.4: `max_iterations` y el agotamiento del presupuesto de llamadas LLM son
# condiciones DISTINTAS con remedios distintos (subir iteraciones vs subir el
# presupuesto de llamadas), así que no deben compartir telemetría.
StopReason = Literal["completed", "max_iterations", "call_budget_exhausted"]


class CallBudget:
    """Contador compartido de llamadas LLM por turno (agente principal + sub-agentes)."""

    __slots__ = ("max_calls", "used")

    def __init__(self, max_calls: int) -> None:
        self.max_calls = max(1, int(max_calls))
        self.used = 0

    def consume(self) -> bool:
        """Consume una llamada; devuelve False si el presupuesto está agotado."""
        if self.used >= self.max_calls:
            return False
        self.used += 1
        return True


ROLE_SYSTEM_PROMPTS: dict[str, str] = {
    "director": (
        "Eres el sub-agente DIRECTOR DE RENDER de ComfyUIPod Studio.\n"
        "Tu única misión es decidir y ejecutar renders con las plantillas curadas, "
        "elegir parámetros (resolución, pasos, CFG, seeds) adecuados para la VRAM "
        "disponible y reportar el resultado (job_id, estado y salidas).\n"
        "Respondes en español, breve y concreto. No delegues a otros roles."
    ),
    "editor": (
        "Eres el sub-agente EDITOR/MONTADOR de ComfyUIPod Studio.\n"
        "Tu misión es construir y ejecutar EDL: proponer cortes con motivo editorial, "
        "transcribir y diarizar cuando falte información, generar highlights y "
        "renderizar entregables 16:9 o 9:16.\n"
        "Respondes en español, breve y concreto. No delegues a otros roles."
    ),
    "artist3d": (
        "Eres el sub-agente ARTISTA VISUAL/3D de ComfyUIPod Studio.\n"
        "Tu misión es preparar assets visuales: indexar medios, buscar planos por "
        "texto o identidad, etiquetar assets y encolar renders de preparación "
        "(keyframes, imágenes base) para pipelines 3D.\n"
        "Respondes en español, breve y concreto. No delegues a otros roles."
    ),
}

SYSTEM_PROMPT_ES = (
    "Eres el asistente de dirección y posproducción de ComfyUIPod Studio.\n"
    "Respondes siempre en español claro y conciso.\n"
    "Tienes acceso a herramientas para:\n"
    "1. `search_assets`: buscar vídeos, imágenes, keyframes o audios dentro del proyecto activo.\n"
    "2. `enqueue_render`: lanzar renders en ComfyUI con las plantillas curadas "
    "(`vace_inpaint`, `vace_restyle`, `wan_flf2v`, `wan_i2v`, `wan_t2v`, `wan_t2v_5b`, "
    "`sdxl_image`, `sdxl_ipadapter`). "
    "Por defecto usa 480p (832x480) para VACE/Wan en 24 GB VRAM.\n"
    "3. `propose_edl`: proponer una lista de decisión de edición (EDL) con cortes "
    "`in_sec`/`out_sec`, motivo editorial y relación de aspecto (`16:9` o `9:16`).\n"
    "4. `extract_keyframes`: extraer keyframes de un clip de vídeo mediante detección de "
    "escenas e intervalo fijo.\n"
    "5. `index_media`: indexar visualmente vídeos e imágenes con embeddings CLIP y rostros "
    "InsightFace en sqlite-vec.\n"
    "6. `find_shots_text`: buscar planos y keyframes por descripción en lenguaje natural "
    "sobre el índice CLIP.\n"
    "7. `find_shots_face`: buscar planos donde aparece una identidad registrada o rostro "
    "de referencia (con filtro opcional de texto CLIP).\n"
    "8. `transcribe`: transcribir audio/vídeo con whisperx (faster-whisper large-v3 + "
    "wav2vec2 ES/EN) con timestamps por palabra.\n"
    "9. `diarize`: ejecutar diarización de hablantes con FunASR cam++ (sin pyannote ni "
    "HF_TOKEN) y fusionar turnos por hablante.\n"
    "10. `build_edl`: renderizar de forma determinista una versión EDL con ffmpeg en "
    "formato `16:9` o `9:16`.\n"
    "11. `list_templates`: listar las plantillas curadas con sus parámetros y defaults.\n"
    "12. `job_status`: consultar el estado de un job de render (queued/running/done/failed/"
    "interrupted) y sus outputs.\n"
    "13. `job_cancel`: cancelar un job en vuelo.\n"
    "14. `tag_asset`: añadir etiquetas a un asset del proyecto para futuras búsquedas.\n"
    "15. `system_status`: consultar salud del pod (GPU, disco, ComfyUI, supervisor).\n"
    "16. `export_edl`: exportar una versión EDL como JSON descargable.\n"
    "17. `update_plan`: crear o actualizar el plan persistente de la tarea actual.\n"
    "18. `delegate_role`: delegar un subobjetivo a un sub-agente especialista "
    "(`director` — renders/plantillas; `editor` — EDL/tiempos; `artist3d` — 3D).\n"
    "Cuando el usuario pida una tarea multi-paso (varios renders, indexación + búsqueda + "
    "montaje, o cualquier trabajo con más de dos herramientas), registra primero un plan "
    "conciso con `update_plan` y ve marcando su progreso; para tareas simples, traza un "
    "plan breve interno, invoca las herramientas necesarias (las independientes pueden "
    "ejecutarse en paralelo) y resume el resultado final. No delegues en `delegate_role` "
    "salvo que el subobjetivo encaje claramente en un rol especialista."
)


@dataclass(slots=True)
class AgentTurnResult:
    """Resultado consolidado de un turno completo del agente."""

    project_id: str
    reply: str
    iterations: int
    stop_reason: StopReason = "completed"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "primary"
    model: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "reply": self.reply,
            "iterations": self.iterations,
            "stop_reason": self.stop_reason,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "provider": self.provider,
            "model": self.model,
        }


class StudioAgent:
    """Orquestador del loop de conversación, memoria y herramientas por proyecto."""

    def __init__(
        self,
        *,
        config: StudioConfig,
        store: ProjectStore,
        comfyui_client: ComfyUIClient,
        broker: ProgressBroker,
        llm_client: OpenAICompatibleLLMClient | None = None,
        tool_belt: ToolBelt | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        redactor: SecretRedactor | None = None,
        system_prompt_override: str | None = None,
        persist_memory: bool = True,
        allow_delegation: bool = True,
        max_llm_calls_per_turn: int = DEFAULT_MAX_LLM_CALLS_PER_TURN,
        budget: CallBudget | None = None,
        agent_role: str | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._comfyui_client = comfyui_client
        self._broker = broker
        self._redactor = redactor or config.redactor()
        self._llm_client = llm_client or OpenAICompatibleLLMClient(
            config=config,
            redactor=self._redactor,
        )
        self._tool_belt = tool_belt or create_default_tool_belt()
        self._max_iterations = max(1, int(max_iterations))
        self._system_prompt_override = system_prompt_override
        self._persist_memory = persist_memory
        self._allow_delegation = allow_delegation
        self._max_llm_calls_per_turn = max(1, int(max_llm_calls_per_turn))
        self._shared_budget = budget
        # M2 (`M3-IDEA-037`): identifica al sub-agente que ejecuta cada herramienta, para
        # que el panel muestre quién hizo qué en un turno delegado.
        self._agent_role = agent_role

        if not any(isinstance(f, RedactingLogFilter) for f in logger.filters):
            logger.addFilter(RedactingLogFilter(self._redactor))

    @property
    def tool_belt(self) -> ToolBelt:
        return self._tool_belt

    @property
    def llm_client(self) -> OpenAICompatibleLLMClient:
        return self._llm_client

    @property
    def max_iterations(self) -> int:
        return self._max_iterations

    def _build_system_message(self, project: dict[str, Any]) -> dict[str, Any]:
        pid = project["id"]
        assets = self._store.list_assets(pid)
        asset_summaries = [
            f"{a['original_filename']} (id={a['id']}, kind={a['kind']}, category={a['category']})"
            for a in assets[:20]
        ]
        latest_edl = self._store.get_latest_edl(pid)
        edl_info = (
            f"v{latest_edl['version']} ({latest_edl['edl'].get('total_duration_sec', 0)}s)"
            if latest_edl
            else "ninguno"
        )
        assets_str = ", ".join(asset_summaries) if asset_summaries else "sin assets aún"

        summary_block = ""
        plan_block = ""
        if self._persist_memory:
            summary = self._store.get_meta(pid, "chat_summary")
            if isinstance(summary, dict) and str(summary.get("text") or "").strip():
                summary_block = (
                    "\n\nResumen de la conversación previa (memoria de largo plazo):\n"
                    f"{str(summary['text']).strip()}"
                )
            plan = self._store.get_active_plan(pid)
            if plan is not None:
                steps_lines = "\n".join(
                    f"  {idx}. [{str(step.get('status') or 'pending')}] "
                    f"{str(step.get('text') or '')}"
                    for idx, step in enumerate(plan.get("steps") or [], start=1)
                )
                plan_block = (
                    f"\n\nPlan activo «{plan.get('title') or 'sin título'}» "
                    f"(id={plan['id']}):\n{steps_lines or '  (sin pasos)'}"
                )

        tools_catalog = "\n".join(
            f"- `{spec.name}`: {spec.description}" for spec in self._tool_belt.list_specs()
        )
        base_prompt = self._system_prompt_override or SYSTEM_PROMPT_ES
        context_block = (
            f"\n\nCatálogo completo de herramientas registradas en el tool-belt:\n"
            f"{tools_catalog}"
            f"\n\nContexto del proyecto activo:\n"
            f"- ID: {pid}\n"
            f"- Nombre: {project.get('name', '')}\n"
            f"- Aspect ratio: {project.get('aspect_ratio', '16:9')}\n"
            f"- Assets ({len(assets)}): {assets_str}\n"
            f"- Último EDL: {edl_info}"
        )
        return {
            "role": "system",
            "content": base_prompt + summary_block + plan_block + context_block,
        }

    def _load_history_messages(self, project_id: str) -> list[dict[str, Any]]:
        if not self._persist_memory:
            return []
        summary = self._store.get_meta(project_id, "chat_summary")
        upto = str(summary.get("upto") or "") if isinstance(summary, dict) else ""
        stored = self._store.list_chat_messages(project_id, limit=60)
        history: list[dict[str, Any]] = []
        for item in stored:
            if upto and str(item.get("created_at") or "") <= upto:
                continue
            role = item.get("role", "user")
            content = str(item.get("content") or "")
            if role in ("user", "assistant"):
                history.append({"role": role, "content": content})
        return history

    async def _maybe_summarize_history(self, project_id: str, budget: CallBudget) -> None:
        """Comprime el historial antiguo en `project_meta.chat_summary` (sin pérdida de la
        ventana reciente). Fallo de LLM ⇒ se conserva el historial textual intacto."""
        if not self._persist_memory:
            return
        stored = self._store.list_chat_messages(project_id, limit=500)
        if len(stored) <= HISTORY_SUMMARY_THRESHOLD:
            return

        older = stored[:-HISTORY_KEEP_RECENT]
        current = self._store.get_meta(project_id, "chat_summary")
        previous_text = str(current.get("text") or "") if isinstance(current, dict) else ""
        transcript = "\n".join(
            f"{item.get('role', '?')}: {str(item.get('content') or '')[:400]}"
            for item in older[-HISTORY_SUMMARY_THRESHOLD:]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Resume en español, en 150 palabras o menos, el estado del proyecto y las "
                    "decisiones tomadas. Integra el resumen previo si existe. No inventes datos "
                    "ni incluyas credenciales."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Resumen previo:\n{previous_text or '(ninguno)'}\n\n"
                    f"Transcripción a resumir:\n{transcript}"
                ),
            },
        ]
        try:
            if not budget.consume():
                return
            resp = await self._llm_client.complete(messages)
            text = self._redactor.redact(str(resp.content or "").strip())
            if text:
                self._store.set_meta(
                    project_id,
                    "chat_summary",
                    {
                        "text": text,
                        "upto": str(older[-1].get("created_at") or ""),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Resumen de historial omitido (se conserva la ventana textual): %s", exc
            )

    def _build_user_message_content(
        self,
        project_id: str,
        user_message: str,
        *,
        asset_ids: Sequence[str] | None = None,
        keyframe_data_urls: Sequence[str] | None = None,
    ) -> str | list[dict[str, Any]]:
        vision_parts: list[dict[str, Any]] = []

        if asset_ids:
            for asset_id in asset_ids:
                asset = self._store.get_asset(project_id, asset_id)
                asset_path = self._store.get_asset_path(project_id, asset_id)
                if asset is None or asset_path is None:
                    raise ValueError(f"Asset de imagen/keyframe no encontrado: {asset_id!r}")
                mime = str(asset.get("mime_type") or "image/png")
                raw_bytes = asset_path.read_bytes()
                vision_parts.append(build_vision_content_part(raw_bytes, mime_type=mime))

        if keyframe_data_urls:
            for data_url in keyframe_data_urls:
                validate_DataUrl_only(data_url)
                vision_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url.strip(), "detail": "auto"},
                    }
                )

        if not vision_parts:
            return user_message

        return [
            {"type": "text", "text": user_message},
            *vision_parts,
        ]

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload = self._redactor.redact_value({"type": event_type, **payload})
        self._broker.publish(event_type, safe_payload)
        return safe_payload

    async def stream_turn(
        self,
        *,
        project_id: str,
        user_message: str,
        asset_ids: Sequence[str] | None = None,
        keyframe_data_urls: Sequence[str] | None = None,
        max_iterations: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Ejecuta un turno emitiendo eventos SSE (`token`, `tool_call`, `tool_result`, `done`)."""
        project = self._store.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")

        user_text = (user_message or "").strip()
        if not user_text:
            raise ValueError("El mensaje del usuario no puede estar vacío.")

        effective_max = (
            max(1, min(int(max_iterations), 25))
            if max_iterations is not None
            else self._max_iterations
        )

        budget = self._shared_budget or CallBudget(self._max_llm_calls_per_turn)
        await self._maybe_summarize_history(project["id"], budget)

        # Construimos historial previo + mensaje actual (con keyframes en base64 si aplica)
        history_messages = self._load_history_messages(project["id"])
        user_content = self._build_user_message_content(
            project["id"],
            user_text,
            asset_ids=asset_ids,
            keyframe_data_urls=keyframe_data_urls,
        )

        messages: list[dict[str, Any]] = [
            self._build_system_message(project),
            *history_messages,
            {"role": "user", "content": user_content},
        ]
        validate_no_auth_urls_in_messages(messages)

        # Persistimos el mensaje del usuario en la memoria SQLite del proyecto (redactado)
        attachments_meta = [
            {"type": "keyframe_asset", "asset_id": aid}
            for aid in (asset_ids or [])
        ]
        if self._persist_memory:
            self._store.add_chat_message(
                project["id"],
                role="user",
                content=self._redactor.redact(user_text),
                tool_calls=self._redactor.redact_value(attachments_meta),
            )

        openai_tools = self._tool_belt.to_openai_tools()

        async def _delegate_runner(
            *,
            role: str,
            task: str,
            max_iterations: int = SUB_AGENT_MAX_ITERATIONS,
        ) -> dict[str, Any]:
            sub_belt = create_restricted_belt(self._tool_belt, role)
            if not sub_belt.list_names():
                raise ValueError(f"El rol {role!r} no tiene herramientas disponibles.")
            sub_agent = StudioAgent(
                config=self._config,
                store=self._store,
                comfyui_client=self._comfyui_client,
                broker=self._broker,
                llm_client=self._llm_client,
                tool_belt=sub_belt,
                max_iterations=min(int(max_iterations), SUB_AGENT_MAX_ITERATIONS),
                redactor=self._redactor,
                system_prompt_override=ROLE_SYSTEM_PROMPTS.get(role, SYSTEM_PROMPT_ES),
                persist_memory=False,
                allow_delegation=False,
                budget=budget,
                agent_role=role,
            )
            sub_result = await sub_agent.run_turn(
                project_id=project["id"],
                user_message=task,
            )
            return {
                "reply": sub_result.reply,
                "iterations": sub_result.iterations,
                "stop_reason": sub_result.stop_reason,
                "tool_calls": [
                    {"name": tc.get("name"), "is_error": False}
                    for tc in sub_result.tool_calls
                ],
                "provider": sub_result.provider,
                "model": sub_result.model,
            }

        tool_ctx = ToolContext(
            project_id=project["id"],
            store=self._store,
            comfyui_client=self._comfyui_client,
            broker=self._broker,
            config=self._config,
            delegate_runner=_delegate_runner if self._allow_delegation else None,
        )

        all_tool_calls: list[dict[str, Any]] = []
        all_tool_results: list[dict[str, Any]] = []
        final_reply = ""
        last_provider = "primary"
        last_model = self._config.studio_llm_model
        stop_reason: StopReason = "completed"
        iterations_run = 0

        for iteration in range(1, effective_max + 1):
            if not budget.consume():
                stop_reason = "call_budget_exhausted"
                if not final_reply:
                    final_reply = (
                        f"Se alcanzó el presupuesto de llamadas LLM por turno "
                        f"({budget.max_calls}); se detuvo el ciclo de herramientas."
                    )
                break
            iterations_run = iteration
            token_queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

            async def _on_token(
                delta: str,
                model_name: str,
                _q: asyncio.Queue[tuple[str, str] | None] = token_queue,
            ) -> None:
                await _q.put((delta, model_name))

            async def _drive_stream(
                _q: asyncio.Queue[tuple[str, str] | None] = token_queue,
            ):
                try:
                    return await self._llm_client.stream_complete(
                        messages=messages,
                        tools=openai_tools,
                        on_token=_on_token,
                    )
                finally:
                    await _q.put(None)

            llm_task = asyncio.create_task(_drive_stream())
            try:
                while True:
                    item = await token_queue.get()
                    if item is None:
                        break
                    piece, piece_model = item
                    ev = await self._emit(
                        "token",
                        {
                            "project_id": project["id"],
                            "iteration": iteration,
                            "delta": piece,
                            "model": piece_model,
                        },
                    )
                    yield ev
                llm_resp = await llm_task
            finally:
                if not llm_task.done():
                    llm_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await llm_task

            last_provider = llm_resp.provider
            last_model = llm_resp.model

            if llm_resp.content:
                final_reply = self._redactor.redact(llm_resp.content)

            if not llm_resp.tool_calls:
                stop_reason = "completed"
                break

            # El asistente solicitó llamadas a herramientas
            assistant_tc_dicts = self._redactor.redact_value(
                [tc.to_openai_dict() for tc in llm_resp.tool_calls]
            )
            redacted_assistant_content = self._redactor.redact(llm_resp.content or "")
            messages.append(
                {
                    "role": "assistant",
                    "content": redacted_assistant_content,
                    "tool_calls": assistant_tc_dicts,
                }
            )
            if self._persist_memory:
                self._store.add_chat_message(
                    project["id"],
                    role="assistant",
                    content=redacted_assistant_content,
                    tool_calls=assistant_tc_dicts,
                )

            # 1) Emitir tool_call en orden original y clasificar paralelizables
            for tc in llm_resp.tool_calls:
                safe_args = self._redactor.redact_value(tc.arguments)
                all_tool_calls.append(
                    {
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": safe_args,
                        "iteration": iteration,
                    }
                )
                call_ev = await self._emit(
                    "tool_call",
                    {
                        "project_id": project["id"],
                        "iteration": iteration,
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "arguments": safe_args,
                        "role": self._agent_role,
                        "delegate_role": (
                            safe_args.get("role") if tc.name == "delegate_role" else None
                        ),
                    },
                )
                yield call_ev

            results_by_call_id: dict[str, tuple[dict[str, Any], bool]] = {}

            async def _execute_one(tc: Any) -> tuple[str, dict[str, Any], bool]:
                is_error = False
                try:
                    raw_res = await self._tool_belt.execute(
                        tc.name,
                        tc.arguments,
                        tool_ctx,
                    )
                    safe_res = self._redactor.redact_value(raw_res)
                except ToolExecutionError as exc:
                    is_error = True
                    safe_res = {
                        "error": self._redactor.redact(str(exc)),
                        "tool": tc.name,
                    }
                except Exception as exc:
                    is_error = True
                    safe_res = {
                        "error": self._redactor.redact(f"Error ejecutando {tc.name}: {exc}"),
                        "tool": tc.name,
                    }
                return tc.id, safe_res, is_error

            # 2) Ejecutar: tools marcadas `parallel_safe` en paralelo (asyncio.gather);
            #    el resto secuencialmente (mutan estado global: interrupt, ffmpeg, plan,
            #    sub-agentes).
            parallel_calls = []
            sequential_calls = []
            for tc in llm_resp.tool_calls:
                spec = self._tool_belt.get(tc.name)
                if spec is not None and spec.parallel_safe:
                    parallel_calls.append(tc)
                else:
                    sequential_calls.append(tc)
            if parallel_calls:
                for call_id, safe_res, is_error in await asyncio.gather(
                    *(_execute_one(tc) for tc in parallel_calls)
                ):
                    results_by_call_id[call_id] = (safe_res, is_error)
            for tc in sequential_calls:
                call_id, safe_res, is_error = await _execute_one(tc)
                results_by_call_id[call_id] = (safe_res, is_error)

            # 3) Emitir tool_result y persistir memoria en el orden original de llamadas
            for tc in llm_resp.tool_calls:
                safe_res, is_error = results_by_call_id.get(
                    tc.id,
                    ({"error": "La herramienta no produjo resultado.", "tool": tc.name}, True),
                )
                tr_record = {
                    "tool_call_id": tc.id,
                    "name": tc.name,
                    "result": safe_res,
                    "is_error": is_error,
                    "iteration": iteration,
                }
                all_tool_results.append(tr_record)
                res_ev = await self._emit(
                    "tool_result",
                    {
                        "project_id": project["id"],
                        "iteration": iteration,
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "result": safe_res,
                        "is_error": is_error,
                        "role": self._agent_role,
                    },
                )
                yield res_ev

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": json.dumps(safe_res, ensure_ascii=False),
                    }
                )
                if self._persist_memory:
                    self._store.add_chat_message(
                        project["id"],
                        role="tool",
                        content=json.dumps(safe_res, ensure_ascii=False),
                        tool_calls=[
                            {"tool_call_id": tc.id, "name": tc.name, "is_error": is_error}
                        ],
                    )
        else:
            stop_reason = "max_iterations"
            if not final_reply:
                final_reply = (
                    f"Se alcanzó el límite máximo de iteraciones ({effective_max}) "
                    "ejecutando herramientas."
                )

        if self._persist_memory:
            self._store.add_chat_message(
                project["id"],
                role="assistant",
                content=self._redactor.redact(final_reply),
                tool_calls=[],
            )

        done_ev = await self._emit(
            "done",
            {
                "project_id": project["id"],
                "reply": final_reply,
                "iterations": iterations_run,
                "stop_reason": stop_reason,
                "tool_calls": all_tool_calls,
                "tool_results": all_tool_results,
                "provider": last_provider,
                "model": last_model,
            },
        )
        yield done_ev

    async def run_turn(
        self,
        *,
        project_id: str,
        user_message: str,
        asset_ids: Sequence[str] | None = None,
        keyframe_data_urls: Sequence[str] | None = None,
        max_iterations: int | None = None,
    ) -> AgentTurnResult:
        """Ejecuta un turno completo y devuelve el resultado consolidado."""
        events: list[dict[str, Any]] = []
        async for ev in self.stream_turn(
            project_id=project_id,
            user_message=user_message,
            asset_ids=asset_ids,
            keyframe_data_urls=keyframe_data_urls,
            max_iterations=max_iterations,
        ):
            events.append(ev)

        done_ev = events[-1] if events else {}
        return AgentTurnResult(
            project_id=project_id,
            reply=str(done_ev.get("reply") or ""),
            iterations=int(done_ev.get("iterations") or 1),
            stop_reason=done_ev.get("stop_reason", "completed"),
            tool_calls=list(done_ev.get("tool_calls") or []),
            tool_results=list(done_ev.get("tool_results") or []),
            provider=str(done_ev.get("provider") or "primary"),
            model=str(done_ev.get("model") or self._config.studio_llm_model),
            events=events,
        )
