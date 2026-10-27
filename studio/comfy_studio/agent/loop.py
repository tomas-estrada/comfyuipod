"""Loop principal del agente LLM de comfy_studio con tool-calling, memoria por proyecto y SSE."""

from __future__ import annotations

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
from .tools import ToolBelt, ToolContext, ToolExecutionError, create_default_tool_belt

logger = logging.getLogger("comfy_studio.agent.loop")

DEFAULT_MAX_ITERATIONS = 8

SYSTEM_PROMPT_ES = (
    "Eres el asistente de dirección y posproducción de ComfyUIPod Studio.\n"
    "Respondes siempre en español claro y conciso.\n"
    "Tienes acceso a herramientas para:\n"
    "1. `search_assets`: buscar vídeos, imágenes, keyframes o audios dentro del proyecto activo.\n"
    "2. `enqueue_render`: lanzar renders en ComfyUI con las plantillas curadas "
    "(`vace_inpaint`, `vace_restyle`, `wan_flf2v`, `wan_i2v`, `wan_t2v`, `sdxl_image`). "
    "Por defecto usa 480p (832x480) para VACE/Wan en 24 GB VRAM.\n"
    "3. `propose_edl`: proponer una lista de decisión de edición (EDL) con cortes "
    "`in_sec`/`out_sec`, motivo editorial y relación de aspecto (`16:9` o `9:16`).\n"
    "Cuando el usuario pida una tarea que requiera consultar el proyecto o ejecutar un "
    "render/montaje, traza un breve plan, invoca las herramientas necesarias y resume el "
    "resultado final."
)


@dataclass(slots=True)
class AgentTurnResult:
    """Resultado consolidado de un turno completo del agente."""

    project_id: str
    reply: str
    iterations: int
    stop_reason: Literal["completed", "max_iterations"] = "completed"
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
        context_block = (
            f"\n\nContexto del proyecto activo:\n"
            f"- ID: {pid}\n"
            f"- Nombre: {project.get('name', '')}\n"
            f"- Aspect ratio: {project.get('aspect_ratio', '16:9')}\n"
            f"- Assets ({len(assets)}): {assets_str}\n"
            f"- Último EDL: {edl_info}"
        )
        return {
            "role": "system",
            "content": SYSTEM_PROMPT_ES + context_block,
        }

    def _load_history_messages(self, project_id: str) -> list[dict[str, Any]]:
        stored = self._store.list_chat_messages(project_id, limit=60)
        history: list[dict[str, Any]] = []
        for item in stored:
            role = item.get("role", "user")
            content = str(item.get("content") or "")
            if role in ("user", "assistant"):
                history.append({"role": role, "content": content})
        return history

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

        # Persistimos el mensaje del usuario en la memoria SQLite del proyecto
        attachments_meta = [
            {"type": "keyframe_asset", "asset_id": aid}
            for aid in (asset_ids or [])
        ]
        self._store.add_chat_message(
            project["id"],
            role="user",
            content=user_text,
            tool_calls=attachments_meta,
        )

        openai_tools = self._tool_belt.to_openai_tools()
        tool_ctx = ToolContext(
            project_id=project["id"],
            store=self._store,
            comfyui_client=self._comfyui_client,
            broker=self._broker,
            config=self._config,
        )

        all_tool_calls: list[dict[str, Any]] = []
        all_tool_results: list[dict[str, Any]] = []
        final_reply = ""
        last_provider = "primary"
        last_model = self._config.studio_llm_model
        stop_reason: Literal["completed", "max_iterations"] = "completed"
        iterations_run = 0

        for iteration in range(1, effective_max + 1):
            iterations_run = iteration
            token_buffer: list[str] = []

            async def _on_token(
                delta: str,
                model_name: str,
                _buf: list[str] = token_buffer,
            ) -> None:
                _buf.append(delta)

            llm_resp = await self._llm_client.stream_complete(
                messages=messages,
                tools=openai_tools,
                on_token=_on_token,
            )
            last_provider = llm_resp.provider
            last_model = llm_resp.model

            for piece in token_buffer:
                ev = await self._emit(
                    "token",
                    {
                        "project_id": project["id"],
                        "iteration": iteration,
                        "delta": piece,
                        "model": llm_resp.model,
                    },
                )
                yield ev

            if llm_resp.content:
                final_reply = llm_resp.content

            if not llm_resp.tool_calls:
                stop_reason = "completed"
                break

            # El asistente solicitó llamadas a herramientas
            assistant_tc_dicts = [tc.to_openai_dict() for tc in llm_resp.tool_calls]
            messages.append(
                {
                    "role": "assistant",
                    "content": llm_resp.content or "",
                    "tool_calls": assistant_tc_dicts,
                }
            )
            self._store.add_chat_message(
                project["id"],
                role="assistant",
                content=llm_resp.content or "",
                tool_calls=assistant_tc_dicts,
            )

            for tc in llm_resp.tool_calls:
                safe_args = self._redactor.redact_value(tc.arguments)
                tc_record = {
                    "id": tc.id,
                    "name": tc.name,
                    "arguments": safe_args,
                    "iteration": iteration,
                }
                all_tool_calls.append(tc_record)
                call_ev = await self._emit(
                    "tool_call",
                    {
                        "project_id": project["id"],
                        "iteration": iteration,
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "arguments": safe_args,
                    },
                )
                yield call_ev

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
                self._store.add_chat_message(
                    project["id"],
                    role="tool",
                    content=json.dumps(safe_res, ensure_ascii=False),
                    tool_calls=[{"tool_call_id": tc.id, "name": tc.name, "is_error": is_error}],
                )
        else:
            stop_reason = "max_iterations"
            if not final_reply:
                final_reply = (
                    f"Se alcanzó el límite máximo de iteraciones ({effective_max}) "
                    "ejecutando herramientas."
                )

        self._store.add_chat_message(
            project["id"],
            role="assistant",
            content=final_reply,
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
