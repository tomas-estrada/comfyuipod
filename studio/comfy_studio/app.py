"""Aplicación FastAPI principal de Comfy Studio (127.0.0.1:18190, /studio/api/*)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, Headers
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from comfy_studio import SIMULATED_ML_NOTICE
from comfy_studio.agent import OpenAICompatibleLLMClient, StudioAgent, create_default_tool_belt
from comfy_studio.agent.tools import EdlCutItem
from comfy_studio.agent_brain.registry import brain_health_report, build_default_brain
from comfy_studio.asr import diarize_project_asset, transcribe_project_asset
from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.config import (
    StudioConfig,
    install_log_redaction,
)
from comfy_studio.edl import execute_edl_ffmpeg, generate_highlights_edl
from comfy_studio.engines.comfyui import ComfyUIEngine
from comfy_studio.engines.editor import EditorEngine
from comfy_studio.engines.health import health_report
from comfy_studio.engines.qa import QaEngine
from comfy_studio.engines.registry import build_default_registry
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.mcp_server import McpContext, create_mcp_router
from comfy_studio.ml import probe_capabilities
from comfy_studio.orchestration.adapters import build_server_collaborators
from comfy_studio.orchestration.health import orchestration_health_report
from comfy_studio.orchestration.service import (
    ProductionNotApprovedError,
    ProductionNotFoundError,
    ProductionService,
)
from comfy_studio.perception import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    INSIGHTFACE_NC_LICENSE_NOTICE,
    ClipVisualIndex,
    InsightFaceIndex,
    PerceptionVectorStore,
    extract_video_keyframes,
)
from comfy_studio.progress import ProgressBroker, format_sse_event
from comfy_studio.projects import MediaValidationError, ProjectStore, sanitize_filename
from comfy_studio.recovery import recover_interrupted_jobs
from comfy_studio.stubs.llm import StubLLMClient
from comfy_studio.subtitles import render_subtitles, subtitle_filename
from comfy_studio.templates import (
    TemplateNotFoundError,
    TemplateParamError,
    TemplateRegistry,
    get_default_registry,
)
from comfy_studio.webhooks import (
    BrokerWebhookDispatcher,
    WebhookNotifier,
    create_webhook_notifier,
)

logger = logging.getLogger("comfy_studio.app")


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default="16:9", max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] | None = None


class ProductionPlanRequest(BaseModel):
    """Cuerpo para planificar una producción (borrador barato, pre-aprobación)."""

    idea: str = Field(..., min_length=1, max_length=4000)
    cost_cap_usd: float = Field(default=0.0, ge=0.0, le=1000.0)


class JobCreateRequest(BaseModel):
    kind: str = Field(default="render", max_length=64)
    project_id: str | None = None
    template_id: str | None = None
    prompt_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class JobProgressRequest(BaseModel):
    status: str | None = None
    prompt_id: str | None = None
    progress_value: int | None = None
    progress_max: int | None = None
    current_node: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class ChatTurnRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    asset_ids: list[str] = Field(default_factory=list)
    keyframe_data_urls: list[str] = Field(default_factory=list)
    stream: bool = Field(default=False)
    max_iterations: int | None = Field(default=None, ge=1, le=25)


class KeyframeExtractRequest(BaseModel):
    asset_id: str | None = None
    interval_sec: float = Field(default=2.0, ge=0.25, le=60.0)
    scene_threshold: float = Field(default=0.25, ge=0.05, le=0.95)
    max_frames: int = Field(default=24, ge=1, le=200)


class PerceptionIndexRequest(BaseModel):
    asset_id: str | None = None
    interval_sec: float = Field(default=2.0, ge=0.25, le=60.0)
    detect_faces: bool = True


class PerceptionSearchRequest(BaseModel):
    query: str | None = None
    text_query: str | None = None
    asset_id: str | None = None
    identity_id: str | None = None
    identity_name: str | None = None
    person_name: str | None = None
    reference_asset_id: str | None = None
    limit: int = Field(default=10, ge=1, le=100)


class FaceIndexRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    reference_asset_id: str | None = None
    keyframe_asset_ids: list[str] | None = None


class IdentityCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    reference_asset_id: str | None = None


class IdentityRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


class TranscribeRequest(BaseModel):
    asset_id: str | None = None
    language: str = Field(default="es", min_length=2, max_length=10)
    script_segments: list[dict[str, Any]] | None = None


class DiarizeRequest(BaseModel):
    asset_id: str | None = None
    transcription_id: str | None = None
    expected_speakers: int | None = Field(default=None, ge=1, le=6)


class EdlCreateRequest(BaseModel):
    title: str = Field(default="Propuesta EDL", min_length=1, max_length=200)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|4:5)$")
    source_asset_id: str | None = None
    # M3-SCR-005: cota dura de cortes para no desencadenar cadenas ilimitadas de
    # subprocess ffmpeg (un corte = 1 recorte + 1 concat)
    cuts: list[EdlCutItem] = Field(..., min_length=1, max_length=200)
    notes: str = Field(default="", max_length=2000)


class EdlHighlightsRequest(BaseModel):
    asset_id: str | None = None
    transcription_id: str | None = None
    title: str = Field(default="Highlights automáticos", min_length=1, max_length=200)
    aspect_ratio: str = Field(default="16:9", pattern=r"^(16:9|9:16|1:1|4:5)$")
    remove_fillers: bool = True
    min_pause_sec: float = Field(default=0.35, ge=0.05, le=10.0)
    max_duration_sec: float | None = Field(default=None, gt=0.0)
    speaker_filter: str | None = None
    # M2 (`M3-IDEA-033`): refinar los cortes con energía acústica real y cambios de escena.
    hybrid: bool = Field(
        default=False,
        description=(
            "Refinar los cortes con la envolvente RMS real del audio y los límites de "
            "escena de los keyframes, en lugar de fiarse solo del texto."
        ),
    )


class EdlBuildRequest(BaseModel):
    edl_id: str | None = None
    version: int | None = Field(default=None, ge=1)
    aspect_ratio: str | None = Field(default=None, pattern=r"^(16:9|9:16|1:1|4:5)$")
    # M2 (`M3-IDEA-036`): un único POST puede encolar varias variantes de entrega.
    # `aspect_ratio` (singular) sigue siendo el caso de una sola variante.
    aspect_ratios: list[str] = Field(
        default_factory=list,
        max_length=4,
        description=(
            "Variantes de entrega a renderizar en lote (16:9, 9:16, 1:1, 4:5). "
            "Si se omite, se usa `aspect_ratio` o el del EDL."
        ),
    )
    source_asset_id: str | None = None
    cuts: list[EdlCutItem] | None = Field(default=None, max_length=200)  # M3-SCR-005
    fps: int = Field(default=24, ge=12, le=60)

    @field_validator("aspect_ratios")
    @classmethod
    def _validate_aspect_ratios(cls, value: list[str]) -> list[str]:
        allowed = {"16:9", "9:16", "1:1", "4:5"}
        cleaned: list[str] = []
        for item in value:
            candidate = str(item or "").strip()
            if candidate not in allowed:
                raise ValueError(
                    f"Relación de aspecto no soportada: {candidate!r} "
                    f"(usa una de {sorted(allowed)})."
                )
            if candidate not in cleaned:
                cleaned.append(candidate)
        return cleaned

    def resolved_aspect_ratios(self) -> list[str]:
        """Lista efectiva de variantes (vacía ⇒ una sola con `aspect_ratio`)."""
        return list(self.aspect_ratios)


def _verify_basic_auth(
    auth_header: str | None,
    expected_user: str,
    expected_pass: str | None,
) -> bool:
    if not expected_pass or not auth_header:
        return False
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    if ":" not in decoded:
        return False
    user, password = decoded.split(":", 1)
    user_ok = secrets.compare_digest(
        user.encode(),
        expected_user.encode(),
    )
    pass_ok = secrets.compare_digest(
        password.encode(),
        expected_pass.encode(),
    )
    return user_ok and pass_ok


_UPLOAD_MEDIA_SUFFIXES = ("/assets", "/media", "/uploads")
_MULTIPART_ENVELOPE_OVERHEAD_BYTES = 64 * 1024  # 64 KiB para cabeceras/boundaries multipart
_MAX_IN_MEMORY_SPOOL_BYTES = 8 * 1024 * 1024  # hasta 8 MiB en memoria antes de spool a disco


def _max_upload_request_bytes(max_upload_bytes: int) -> int:
    return max_upload_bytes + _MULTIPART_ENVELOPE_OVERHEAD_BYTES


def _is_media_upload_request(request: Request) -> bool:
    if request.method.upper() != "POST":
        return False
    path = request.url.path.rstrip("/")
    if not path.startswith("/studio/api/projects/"):
        return False
    rest = path[len("/studio/api/projects/") :]
    parts = [part for part in rest.split("/") if part]
    return len(parts) == 2 and f"/{parts[1]}" in _UPLOAD_MEDIA_SUFFIXES


def _check_upload_content_length(
    headers: Headers,
    max_upload_bytes: int,
    max_total_bytes: int | None = None,
) -> None:
    raw_cl = headers.get("content-length")
    if raw_cl is None:
        return
    try:
        content_length = int(raw_cl.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Cabecera Content-Length inválida",
        ) from exc
    if content_length < 0:
        raise HTTPException(
            status_code=400,
            detail="Cabecera Content-Length inválida",
        )
    per_file_ceiling = _max_upload_request_bytes(max_upload_bytes)
    total_ceiling = (
        _max_upload_request_bytes(max_total_bytes) if max_total_bytes is not None else None
    )
    ceiling = (
        min(per_file_ceiling, total_ceiling) if total_ceiling is not None else per_file_ceiling
    )
    if content_length > ceiling:
        is_total_bound = total_ceiling is not None and total_ceiling < per_file_ceiling
        raise HTTPException(
            status_code=413,
            detail=(
                f"La petición supera el límite TOTAL de subida ({max_total_bytes} bytes)"
                if is_total_bound
                else f"El archivo supera el límite permitido ({max_upload_bytes} bytes)"
            ),
        )


async def _bounded_request_stream(
    request: Request,
    *,
    max_stream_bytes: int,
    max_upload_bytes: int,
    total_bound: bool = False,
) -> AsyncGenerator[bytes, None]:
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > max_stream_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"La petición supera el límite TOTAL de subida ({max_upload_bytes} bytes)"
                    if total_bound
                    else f"El archivo supera el límite permitido ({max_upload_bytes} bytes)"
                ),
            )
        yield chunk


class _BoundedMultiPartParser(MultiPartParser):
    """Parser multipart que acota los bytes por archivo ANTES de volcar a disco temporal."""

    def __init__(
        self,
        headers: Headers,
        stream: AsyncGenerator[bytes, None],
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,
        max_file_size: int,
        max_total_file_bytes: int | None = None,
    ) -> None:
        super().__init__(
            headers,
            stream,
            max_files=max_files,
            max_fields=max_fields,
            max_part_size=max_part_size,
        )
        self.max_file_size = max_file_size
        self.max_total_file_bytes = max_total_file_bytes
        self.spool_max_size = min(
            max(max_file_size, 1024 * 1024),
            _MAX_IN_MEMORY_SPOOL_BYTES,
        )
        # Contador de la parte ACTUAL (no un dict por `id()`: CPython reutiliza la
        # dirección de los objetos liberados, así que las partes heredaban el recuento
        # de las anteriores y una subida de N partes bajo el límite fallaba con 413).
        self._current_part_bytes = 0
        self._total_file_bytes = 0

    def on_part_begin(self) -> None:
        self._current_part_bytes = 0
        super().on_part_begin()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            chunk_len = end - start
            self._current_part_bytes += chunk_len
            if self._current_part_bytes > self.max_file_size:
                raise HTTPException(
                    status_code=413,
                    detail=f"El archivo supera el límite permitido ({self.max_file_size} bytes)",
                )
            # M1.7: techo del TOTAL de bytes de archivo de la petición (M3-SEC-003).
            self._total_file_bytes += chunk_len
            if (
                self.max_total_file_bytes is not None
                and self._total_file_bytes > self.max_total_file_bytes
            ):
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "La petición supera el límite TOTAL de subida "
                        f"({self.max_total_file_bytes} bytes); divide la subida en varias "
                        "peticiones."
                    ),
                )
        super().on_part_data(data, start, end)

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except OSError as exc:
            raise HTTPException(
                status_code=413,
                detail=(
                    "El archivo supera el límite permitido o agotó el almacenamiento "
                    f"temporal ({self.max_file_size} bytes)"
                ),
            ) from exc


def _install_bounded_form_parser(
    request: Request,
    max_upload_bytes: int,
    max_total_bytes: int | None = None,
) -> None:
    # M1.7 (M3-SEC-003): el techo por archivo no acota el TOTAL de la petición. Con el
    # stream de red acotado sólo cuando llega `Content-Length`, un cliente chunked podía
    # enviar N partes bajo el límite y consumir N × límite de memoria/disco temporal.
    per_file_stream_bytes = _max_upload_request_bytes(max_upload_bytes)
    total_stream_bytes = (
        _max_upload_request_bytes(max_total_bytes) if max_total_bytes is not None else None
    )
    total_bound = total_stream_bytes is not None and total_stream_bytes < per_file_stream_bytes
    max_stream_bytes = (
        min(per_file_stream_bytes, total_stream_bytes)
        if total_stream_bytes is not None
        else per_file_stream_bytes
    )
    # El detalle del 413 de stream debe citar el límite que realmente se cruzó.
    stream_error_bytes = max_total_bytes if total_bound else max_upload_bytes

    async def _bounded_get_form(
        *,
        max_files: int | float = 1000,
        max_fields: int | float = 1000,
        max_part_size: int = 1024 * 1024,
    ) -> FormData:
        if getattr(request, "_form", None) is None:
            _check_upload_content_length(
                request.headers, max_upload_bytes, max_total_bytes
            )
            content_type_header = request.headers.get("Content-Type")
            content_type, _ = parse_options_header(content_type_header)
            if content_type == b"multipart/form-data":
                try:
                    async with aclosing(
                        _bounded_request_stream(
                            request,
                            max_stream_bytes=max_stream_bytes,
                            max_upload_bytes=stream_error_bytes,
                            total_bound=total_bound,
                        )
                    ) as stream:
                        multipart_parser = _BoundedMultiPartParser(
                            request.headers,
                            stream,
                            max_files=max_files,
                            max_fields=max_fields,
                            max_part_size=max_part_size,
                            max_file_size=max_upload_bytes,
                            max_total_file_bytes=max_total_bytes,
                        )
                        request._form = await multipart_parser.parse()
                except MultiPartException as exc:
                    if "app" in request.scope:
                        raise HTTPException(status_code=400, detail=exc.message) from exc
                    raise exc
            elif content_type == b"application/x-www-form-urlencoded":
                try:
                    async with aclosing(
                        _bounded_request_stream(
                            request,
                            max_stream_bytes=max_stream_bytes,
                            max_upload_bytes=stream_error_bytes,
                            total_bound=total_bound,
                        )
                    ) as stream:
                        form_parser = FormParser(
                            request.headers,
                            stream,
                            max_fields=max_fields,
                            max_part_size=max_part_size,
                        )
                        request._form = await form_parser.parse()
                except MultiPartException as exc:
                    if "app" in request.scope:
                        raise HTTPException(status_code=400, detail=exc.message) from exc
                    raise exc
            else:
                request._form = FormData()
        return request._form  # type: ignore[return-value]

    request._get_form = _bounded_get_form  # type: ignore[method-assign]


class _BoundedUploadAPIRoute(APIRoute):
    """APIRoute que aplica recepción acotada antes de que FastAPI resuelva request.form()."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            if _is_media_upload_request(request):
                active_cfg: StudioConfig = request.app.state.config
                limit = active_cfg.max_upload_bytes
                _check_upload_content_length(request.headers, limit)
                _install_bounded_form_parser(
                    request,
                    limit,
                    max_total_bytes=active_cfg.upload_max_total_bytes,
                )
            return await original_route_handler(request)

        return custom_route_handler


def _edge_auth_effective_pass(cfg: StudioConfig) -> str | None:
    """Contraseña exigible por el edge (config o env), o `None` si no hay ninguna."""
    env_pass = (
        os.environ.get("COMFY_PASS", "").strip()
        or os.environ.get("STUDIO_AUTH_PASS", "").strip()
        or None
    )
    return cfg.get_comfy_pass() or env_pass


def _edge_auth_user(cfg: StudioConfig) -> str:
    return (
        os.environ.get("COMFY_USER", "").strip()
        or os.environ.get("STUDIO_AUTH_USER", "").strip()
        or cfg.comfy_user
    )


def _edge_auth_enforced(cfg: StudioConfig) -> bool:
    """True si el edge exigirá Basic Auth en las rutas /studio y /health."""
    require_auth = (
        cfg.require_auth
        or bool(_edge_auth_effective_pass(cfg))
        or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
    )
    return bool(require_auth and _edge_auth_effective_pass(cfg))


async def _probe_comfyui(
    comfyui_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    target = f"{comfyui_url.rstrip('/')}/system_stats"
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(2.0, connect=1.0),
        ) as client:
            resp = await client.get(target)
            if resp.status_code == 200:
                data = resp.json()
                return True, data if isinstance(data, dict) else {}
    except Exception:
        pass
    return False, None


def _publish_transcription_event(
    broker: ProgressBroker,
    project_id: str,
    transcription: dict[str, Any],
) -> None:
    """Publica `transcription` en el broker (SSE del panel + webhook `transcription_done`)."""
    segments = list(transcription.get("segments") or [])
    words = list(transcription.get("words") or [])
    broker.publish(
        "transcription",
        {
            "project_id": project_id,
            "transcription_id": transcription.get("id"),
            "asset_id": transcription.get("asset_id"),
            "language": transcription.get("language"),
            "segments_count": len(segments),
            "words_count": len(words),
            "diarized": bool(transcription.get("diarized")),
            "backend": transcription.get("backend"),
            "simulated": bool(transcription.get("simulated", True)),
        },
    )


def _require_perception(request: Request) -> None:
    """503 explícito cuando la percepción vectorial no está disponible (M1.2).

    En modo `real` (STUDIO_PERCEPTION_BACKEND=real) no hay fallback: si el probe
    ML falló en el arranque, los endpoints responden 503 aunque sqlite-vec esté
    presente.
    """
    if getattr(request.app.state, "perception_available", False):
        backend = getattr(request.app.state, "perception_backend", "sim")
        probe = getattr(request.app.state, "perception_probe_result", {}) or {}
        if backend == "real" and not probe.get("probe_ok", False):
            reason = probe.get("reason") or "backends ML no disponibles"
            raise HTTPException(
                status_code=503,
                detail=(
                    "Percepción vectorial en modo REAL pero los backends ML no "
                    f"están disponibles. Motivo: {reason}"
                ),
            )
        return
    reason = getattr(request.app.state, "perception_error", None) or "no disponible"
    raise HTTPException(
        status_code=503,
        detail=(
            "Percepción vectorial no disponible en este pod; el resto de Studio sigue "
            f"operativo y el índice requiere `sqlite-vec`. Motivo: {reason}"
        ),
    )


def create_app(
    config: StudioConfig | None = None,
    *,
    comfyui_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = config if config is not None else StudioConfig.from_env()
    redactor = cfg.redactor()
    install_log_redaction(redactor)
    install_log_redaction(redactor, logger=logging.getLogger("comfy_studio"))

    # M3-SEC-002: sin contraseña exigible no hay lockout posible (ver test de
    # regresión), pero la postura fail-open debe ser VISIBLE: WARN en arranque +
    # `edge_auth: "open"` en /studio/api/health.
    if _edge_auth_effective_pass(cfg) is None and (
        cfg.require_auth
        or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
    ):
        logger.warning(
            "STUDIO: require_auth activo pero sin COMFY_PASS/STUDIO_AUTH_PASS exigible; "
            "los endpoints /studio se sirven SIN autenticación (fail-open visible, "
            "edge_auth=open en /studio/api/health). Define COMFY_PASS para activar Basic Auth."
        )

    store = ProjectStore(cfg.studio_data_dir)
    broker = ProgressBroker(redactor=redactor)
    stub_llm = StubLLMClient(model=cfg.studio_llm_model) if cfg.studio_stub_llm else None

    webhook_notifier: WebhookNotifier | None = create_webhook_notifier(cfg)
    webhook_dispatcher: BrokerWebhookDispatcher | None = (
        BrokerWebhookDispatcher(broker, webhook_notifier)
        if webhook_notifier is not None
        else None
    )

    @asynccontextmanager
    async def _webhook_lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        if webhook_notifier is not None:
            webhook_notifier.start()
        if webhook_dispatcher is not None:
            webhook_dispatcher.start()

        async def _run_job_recovery() -> None:
            try:
                report = await recover_interrupted_jobs(
                    store=store,
                    comfyui_client=_app.state.comfyui_client,
                    broker=broker,
                    resume=cfg.studio_resume_jobs,
                    start_delay_s=(
                        cfg.studio_resume_delay_s if cfg.studio_resume_jobs else 0.0
                    ),
                )
                if report.get("stale"):
                    logger.info("Recuperación de jobs tras reinicio: %s", report)
            except Exception:  # noqa: BLE001
                logger.exception("Error en la recuperación de jobs interrumpidos")

        recovery_task: asyncio.Task[None] = asyncio.create_task(_run_job_recovery())
        try:
            yield
        finally:
            # M1.6: drenar la recuperación acotadamente en vez de cancelarla en seco. Un
            # resume a mitad de `execute_prompt_job` quedaba en estado no terminal sin
            # reconciliación; ahora se le da `STUDIO_RECOVERY_DRAIN_S` y, si no termina,
            # el job queda `interrupted` antes de cancelar.
            if not recovery_task.done():
                drain_s = max(0.0, float(cfg.studio_recovery_drain_s))
                try:
                    await asyncio.wait_for(asyncio.shield(recovery_task), timeout=drain_s)
                except (TimeoutError, asyncio.CancelledError):
                    recovery_task.cancel()
                    try:
                        await recovery_task
                    except asyncio.CancelledError:
                        pass
                    logger.warning(
                        "STUDIO: la recuperación de jobs no terminó en %.0fs; tarea cancelada.",
                        drain_s,
                    )
            if webhook_dispatcher is not None:
                await webhook_dispatcher.stop()
            if webhook_notifier is not None:
                await webhook_notifier.stop()

    app = FastAPI(
        title="Comfy Studio Server",
        version="0.1.0",
        lifespan=_webhook_lifespan,
    )
    app.router.route_class = _BoundedUploadAPIRoute
    app.state.config = cfg
    app.state.redactor = redactor
    app.state.store = store
    app.state.broker = broker
    app.state.stub_llm = stub_llm
    app.state.webhook_notifier = webhook_notifier
    app.state.webhook_dispatcher = webhook_dispatcher
    app.state.comfyui_transport = comfyui_transport
    app.state.comfyui_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=comfyui_transport,
        store=store,
        broker=broker,
        redactor=redactor,
    )
    # (El ProductionService se arma más abajo, tras construir app.state.llm_client,
    # porque el cableado de motores reales lo necesita — M6-AGT-001.)
    # M1.2: la construcción de la percepción vectorial NO puede tumbar la app. Si
    # `sqlite-vec` falta o falla, se degrada a `perception_available=False`: los
    # endpoints de percepción responden 503 con el motivo y el resto de Studio
    # (proyectos, chat, renders, galería) sigue funcionando.
    #
    # M3.2: `STUDIO_PERCEPTION_BACKEND` (auto|real|sim) selecciona el backend:
    #   - sim: rutas deterministas actuales, `simulated: true`, sin probe.
    #   - auto: probe al arranque; si pasa -> real, si no -> sim (WARN visible).
    #   - real: sin fallback; si el probe falla, perception_available=False y
    #     los endpoints responden 503 con el motivo.
    perception_error: str | None = None
    vector_store: PerceptionVectorStore | None = None
    perception_backend: str = "sim"
    probe_result: dict[str, Any] = {}
    try:
        vector_store = PerceptionVectorStore(cfg.studio_data_dir / "index.db")
    except Exception as exc:  # noqa: BLE001 - cualquier fallo degrada solo percepción
        perception_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "STUDIO: percepción vectorial DESHABILITADA (%s). Los endpoints de percepción "
            "responderán 503; el resto de Studio sigue operativo.",
            redactor.redact(perception_error),
        )

    requested_backend = (cfg.studio_perception_backend or "auto").strip().lower()
    if requested_backend == "sim":
        perception_backend = "sim"
    elif requested_backend == "real":
        # Fallo cerrado: sin fallback a sim. Ejecutar probe para reportar motivo.
        caps = probe_capabilities(redactor=redactor)
        probe_result = caps.to_public_dict()
        if caps.probe_ok and vector_store is not None:
            perception_backend = "real"
        else:
            perception_backend = "real"
            if not caps.probe_ok:
                perception_error = probe_result.get("reason") or "probe no pasó"
                if vector_store is not None:
                    perception_error = (perception_error or "") + " (sqlite-vec OK)"
                logger.warning(
                    "STUDIO: percepción en modo REAL pero el probe ML falló (%s). "
                    "Los endpoints de percepción responderán 503; sin fallback a sim.",
                    redactor.redact(perception_error),
                )
            if vector_store is None:
                perception_error = (perception_error or "") + "; sqlite-vec no disponible"
    else:
        # auto: probe al arranque; real si pasa, sim si no (fallback visible).
        caps = probe_capabilities(redactor=redactor)
        probe_result = caps.to_public_dict()
        if caps.probe_ok and vector_store is not None:
            perception_backend = "real"
        else:
            perception_backend = "sim"
            if not caps.probe_ok:
                logger.warning(
                    "STUDIO: percepción en modo SIMULACIÓN (fallback de auto; "
                    "probe ML: %s). Usa STUDIO_PERCEPTION_BACKEND=real para exigir "
                    "backends reales o =sim para silenciar este aviso.",
                    redactor.redact(probe_result.get("reason") or "probe no pasó"),
                )

    app.state.perception_available = vector_store is not None
    app.state.perception_error = perception_error
    app.state.perception_backend = perception_backend
    app.state.perception_probe_result = probe_result
    app.state.vector_store = vector_store

    # M3.3: comparar versión del índice almacenada con las dims activas.
    # Desajuste (p.ej. 64 -> 768/512 al migrar a backends reales) ⇒ stale:
    # lecturas devuelven vacío con stale=true, escrituras rechazan 409.
    perception_stale = False
    if vector_store is not None:
        stored_clip, stored_face = vector_store.stored_dims()
        active_clip = CLIP_EMBED_DIM
        active_face = FACE_EMBED_DIM
        if stored_clip is not None and (stored_clip != active_clip or stored_face != active_face):
            perception_stale = True
            logger.warning(
                "STUDIO: índice de percepción STALE (dims almacenados clip=%s face=%s, "
                "activas clip=%s face=%s). Lecturas devolverán vacío con stale=true; "
                "escrituras rechazan 409. Ejecuta POST /studio/api/perception/reindex.",
                stored_clip, stored_face, active_clip, active_face,
            )
        elif stored_clip is None and vector_store.get_schema_meta() == {}:
            # Índice nuevo: registrar meta de versión en el primer alta.
            pass
    app.state.perception_stale = perception_stale
    app.state.clip_index = (
        ClipVisualIndex(
            store,
            vector_store,
            ffmpeg_timeout_s=cfg.studio_ffmpeg_timeout_s,
            ffprobe_timeout_s=cfg.studio_ffprobe_timeout_s,
        )
        if vector_store
        else None
    )
    app.state.face_index = InsightFaceIndex(store, vector_store) if vector_store else None
    app.state.template_registry = get_default_registry()
    app.state.tool_belt = create_default_tool_belt()
    app.state.llm_client = OpenAICompatibleLLMClient(
        config=cfg,
        stub_client=stub_llm,
        redactor=redactor,
    )
    app.state.agent = StudioAgent(
        config=cfg,
        store=store,
        comfyui_client=app.state.comfyui_client,
        broker=broker,
        llm_client=app.state.llm_client,
        tool_belt=app.state.tool_belt,
        redactor=redactor,
    )

    # Servicio de producción (orquestación M6). La StoryboardGate es la puerta
    # de gasto humano: approve_plan NUNCA se expone al agente LLM (ver
    # mcp_server.py / M6-AGT-001), solo la API pública la acciona.
    #
    # Colaboradores: por defecto STUB (CI/tests sin GPU). En el pod, con
    # STUDIO_ORCH_REAL_ENGINES=1, se cablean los adaptadores reales a los
    # motores vivos (requiere llm_client ya construido, de ahí esta posición).
    # El cableado es TOLERANTE a fallos: si construir un motor real falla, esa
    # fase cae a su stub y el servicio arranca igual (nunca tumba el pod).
    orch_collaborators = None
    if (os.environ.get("STUDIO_ORCH_REAL_ENGINES") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        try:
            orch_collaborators = build_server_collaborators(
                llm_client=app.state.llm_client,
                engines={
                    "comfyui": ComfyUIEngine(app.state.comfyui_client),
                    "upscaler": UpscalerEngine(),
                },
                qa_engine=QaEngine(),
                editor_engine=EditorEngine(backend="remotion"),
                template_registry=app.state.template_registry,
            )
            logger.info(
                "STUDIO: orquestación con motores REALES (STUDIO_ORCH_REAL_ENGINES=1)."
            )
        except Exception as exc:  # noqa: BLE001 - nunca tumbar el arranque
            logger.warning(
                "STUDIO: no se pudieron cablear los motores reales de orquestación "
                "(%s); se usan stubs.",
                redactor.redact(str(exc)),
            )
            orch_collaborators = None
    app.state.production_service = ProductionService(collaborators=orch_collaborators)

    mcp_ctx = McpContext(
        store=store,
        comfyui_client=app.state.comfyui_client,
        broker=broker,
        config=cfg,
        tool_belt=app.state.tool_belt,
        redactor=redactor,
        production_service=app.state.production_service,
    )
    app.state.mcp_context = mcp_ctx
    app.include_router(create_mcp_router(mcp_ctx))

    @app.middleware("http")
    async def edge_auth_and_redaction_middleware(request: Request, call_next):
        active_cfg: StudioConfig = request.app.state.config
        env_user = _edge_auth_user(active_cfg)
        effective_pass = _edge_auth_effective_pass(active_cfg)
        require_auth = (
            active_cfg.require_auth
            or bool(effective_pass)
            or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
            or request.headers.get("X-Studio-Simulate-Edge") == "1"
        )

        # Sin contraseña configurada no hay credencial válida posible: exigir auth
        # bloquearía todas las peticiones (lockout total). Se omite la exigencia
        # (fail-open VISIBLE: WARN en arranque + edge_auth=open en health, M3-SEC-002).
        if require_auth and effective_pass and request.url.path.startswith(("/studio", "/health")):
            auth_header = request.headers.get("Authorization")
            # M3-C: los clientes máquina del endpoint MCP pueden autenticarse con
            # `Authorization: Bearer <STUDIO_API_TOKEN>` en lugar de Basic Auth
            # (nginx sigue exigiendo Basic Auth en el borde público).
            mcp_bearer_ok = False
            api_token = active_cfg.get_api_token()
            if (
                api_token
                and request.url.path.startswith("/studio/api/mcp")
                and auth_header
            ):
                parts = auth_header.split()
                mcp_bearer_ok = (
                    len(parts) == 2
                    and parts[0].lower() == "bearer"
                    and secrets.compare_digest(parts[1].encode("utf-8"), api_token.encode("utf-8"))
                )
            if not mcp_bearer_ok and not _verify_basic_auth(auth_header, env_user, effective_pass):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Credenciales requeridas"},
                    headers={"WWW-Authenticate": 'Basic realm="ComfyUI Vast.ai Access"'},
                )

        if _is_media_upload_request(request):
            try:
                _check_upload_content_length(
                    request.headers,
                    active_cfg.max_upload_bytes,
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )

        response = await call_next(request)

        # M3-SCR-008 (regla 3 de AGENTS.md): TODA respuesta JSON de /studio y /health pasa
        # por SecretRedactor de forma centralizada, incluidos `detail=str(exc)`
        # de HTTPException y dicts crudos de los endpoints M3 (percepción, ASR,
        # EDL). SSE, archivos y media quedan fuera (no son application/json).
        if request.url.path.startswith(("/studio", "/health")):
            content_type = response.headers.get("content-type", "")
            if content_type.startswith("application/json"):
                body = b"".join([chunk async for chunk in response.body_iterator])
                if body:
                    redacted = active_cfg.redactor().redact(body.decode("utf-8"))
                    headers = dict(response.headers)
                    headers["content-length"] = str(len(redacted.encode("utf-8")))
                    return Response(
                        content=redacted,
                        status_code=response.status_code,
                        headers=headers,
                        media_type="application/json",
                    )
        return response

    @app.get("/studio/api/health")
    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store
        active_redactor = active_cfg.redactor()
        reachable, stats = await _probe_comfyui(
            active_cfg.studio_comfyui_url,
            transport=request.app.state.comfyui_transport,
        )
        perception_available = bool(getattr(request.app.state, "perception_available", False))
        perception_error = getattr(request.app.state, "perception_error", None)
        payload = {
            "status": "ok",
            "server": "ok",
            "comfyui": "ok" if reachable else "degraded",
            "comfyui_reachable": reachable,
            "comfyui_url": active_cfg.studio_comfyui_url,
            "comfyui_devices": len((stats or {}).get("devices", [])) if stats else 0,
            "model_profile": active_cfg.model_profile,
            "edge_auth": "basic" if _edge_auth_enforced(active_cfg) else "open",
            "stub_llm": active_cfg.studio_stub_llm,
            "llm_model": active_cfg.studio_llm_model,
            "llm_key_status": active_cfg.masked_llm_key_status(),
            "llm_fallback_key_status": active_cfg.masked_llm_fallback_key_status(),
            # M1.2/M3: la percepción es un subsistema degradable e independiente del
            # resto de Studio; su estado debe ser observable sin abrir un log.
            "perception": {
                "available": perception_available,
                "backend": getattr(request.app.state, "perception_backend", "sim"),
                "reason": perception_error,
                "simulated": getattr(request.app.state, "perception_backend", "sim") == "sim",
                "stale": bool(getattr(request.app.state, "perception_stale", False)),
                "probe": getattr(request.app.state, "perception_probe_result", None),
            },
            "schema_version": active_store.get_schema_version(),
            # M5-F7: estado por motor del roster (ready/stub/unwired) para que el
            # strict-check del supervisor observe cada motor, no solo que el studio vive.
            "engines": health_report(build_default_registry()),
            # M6-B1: estado del cerebro del agente (memory/compress/retrieve/rerank).
            "agent_brain": brain_health_report(build_default_brain()),
            # M6-B5: estado de la capa de orquestación (grafo/servicio/LangGraph).
            "orchestration": orchestration_health_report(),
        }
        return active_redactor.redact_value(payload)

    @app.get("/studio/api/status")
    async def studio_status(request: Request) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store
        active_redactor = active_cfg.redactor()
        reachable, stats = await _probe_comfyui(
            active_cfg.studio_comfyui_url,
            transport=request.app.state.comfyui_transport,
        )
        projects_list = active_store.list_projects()
        jobs_list = active_store.list_jobs()
        payload = {
            "status": "ok",
            "comfyui": {
                "status": "ok" if reachable else "degraded",
                "reachable": reachable,
                "url": active_cfg.studio_comfyui_url,
                "system_stats": stats,
            },
            "db": {
                "schema_version": active_store.get_schema_version(),
                "projects_count": len(projects_list),
                "jobs_count": len(jobs_list),
            },
            "llm": {
                "base_url": active_cfg.studio_llm_base_url,
                "model": active_cfg.studio_llm_model,
                "fallback_base_url": active_cfg.studio_llm_fallback_base_url,
                "fallback_model": active_cfg.studio_llm_fallback_model,
                "key_status": active_cfg.masked_llm_key_status(),
                "fallback_key_status": active_cfg.masked_llm_fallback_key_status(),
                "stub": active_cfg.studio_stub_llm,
            },
            "env_masked": active_cfg.to_public_dict(),
        }
        return active_redactor.redact_value(payload)

    # --- CRUD Proyectos ---

    @app.post("/studio/api/projects", status_code=201)
    async def create_project(
        body: ProjectCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        try:
            return active_store.create_project(
                name=body.name,
                description=body.description,
                aspect_ratio=body.aspect_ratio,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/studio/api/projects")
    async def list_projects(request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        return {"projects": active_store.list_projects()}

    @app.get("/studio/api/projects/{project_id}")
    async def get_project(project_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        proj = active_store.get_project(project_id)
        if proj is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return proj

    @app.patch("/studio/api/projects/{project_id}")
    @app.put("/studio/api/projects/{project_id}")
    async def update_project(
        project_id: str,
        body: ProjectUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        try:
            updated = active_store.update_project(
                project_id,
                name=body.name,
                description=body.description,
                aspect_ratio=body.aspect_ratio,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return updated

    @app.delete("/studio/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        deleted = active_store.delete_project(project_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return {"deleted": True, "id": project_id}

    # --- Producciones (orquestación M6) ---
    #
    # GOBERNANZA (M6-AGT-001): la puerta de gasto humano. `plan` deja la
    # producción en `awaiting_approval` SIN gastar GPU; el lazo caro (ejecución
    # + ensamblaje) solo corre tras `approve`, y `approve` SOLO vive aquí, en la
    # API pública accionada por el usuario. El agente LLM NUNCA ve `approve`
    # (ver BELT_TOOLS_EXPOSED en mcp_server.py). `approve` despacha el lazo como
    # tarea de fondo (run_in_threadpool) para no bloquear Nginx (504).

    @app.post("/studio/api/productions", status_code=201)
    async def plan_production_route(
        body: ProductionPlanRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(
                service.plan_production,
                body.idea,
                cost_cap_usd=body.cost_cap_usd,
            )
        except Exception as exc:  # noqa: BLE001 - errores de planificación -> 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/studio/api/productions/{production_id}")
    async def get_production_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(service.get_plan, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/studio/api/productions/{production_id}/status")
    async def production_status_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(service.production_status, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/studio/api/productions/{production_id}/approve")
    async def approve_production_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        """Aprobación HUMANA de la puerta de gasto: corre el lazo caro de fondo."""
        service: ProductionService = request.app.state.production_service
        try:
            # corre el grafo caro fuera del event loop para no bloquear Nginx.
            return await run_in_threadpool(service.approve_plan, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionNotApprovedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # --- Subida y gestión de medios ---

    @app.post("/studio/api/projects/{project_id}/assets", status_code=201)
    @app.post("/studio/api/projects/{project_id}/media", status_code=201)
    @app.post("/studio/api/projects/{project_id}/uploads", status_code=201)
    async def upload_project_media(
        project_id: str,
        request: Request,
        file: UploadFile = File(...),
        category: str = Form("uploads"),
    ) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store

        # Lectura acotada a max_upload_bytes + 1 para rechazar sin agotar memoria
        limit = active_cfg.max_upload_bytes
        data = await file.read(limit + 1)
        try:
            return active_store.save_upload(
                project_id,
                filename=file.filename,
                content_type=file.content_type,
                data=data,
                max_upload_bytes=limit,
                category=category,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    @app.get("/studio/api/projects/{project_id}/assets")
    @app.get("/studio/api/projects/{project_id}/media")
    async def list_project_assets(
        project_id: str,
        request: Request,
        kind: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return {
            "assets": active_store.list_assets(
                project_id,
                kind=kind,
                category=category,
            )
        }

    @app.get("/studio/api/projects/{project_id}/assets/{asset_id}")
    @app.get("/studio/api/projects/{project_id}/media/{asset_id}")
    async def get_project_asset(
        project_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="Medio no encontrado")
        return asset

    @app.get("/studio/api/projects/{project_id}/assets/{asset_id}/file")
    @app.get("/studio/api/projects/{project_id}/media/{asset_id}/file")
    async def download_project_asset_file(
        project_id: str,
        asset_id: str,
        request: Request,
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        path = active_store.get_asset_path(project_id, asset_id)
        if asset is None or path is None:
            raise HTTPException(status_code=404, detail="Archivo de medio no encontrado")
        disposition = "attachment" if download else "inline"
        return Response(
            content=path.read_bytes(),
            media_type=asset["mime_type"],
            headers={
                "Content-Disposition": f'{disposition}; filename="{asset["original_filename"]}"'
            },
        )

    @app.get("/studio/api/projects/{project_id}/gallery")
    async def list_project_gallery(
        project_id: str,
        request: Request,
        kind: str | None = Query(default=None),
        category: str = Query(default="outputs"),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        eff_category = None if category == "all" else category
        items = active_store.list_assets(
            project_id,
            kind=kind,
            category=eff_category,
        )
        return {
            "project_id": project_id,
            "assets": items,
            "outputs": items,
        }

    @app.get("/studio/api/view")
    async def view_media_output(
        request: Request,
        project_id: str | None = Query(default=None),
        asset_id: str | None = Query(default=None),
        filename: str | None = Query(default=None),
        subfolder: str = Query(default=""),
        type: str = Query(default="output"),
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        disposition = "attachment" if download else "inline"

        if project_id and asset_id:
            asset = active_store.get_asset(project_id, asset_id)
            path = active_store.get_asset_path(project_id, asset_id)
            if asset is None or path is None:
                raise HTTPException(status_code=404, detail="Asset de galería no encontrado")
            return Response(
                content=path.read_bytes(),
                media_type=asset["mime_type"],
                headers={
                    "Content-Disposition": (
                        f'{disposition}; filename="{asset["original_filename"]}"'
                    )
                },
            )

        if project_id and filename:
            for candidate in active_store.list_assets(project_id):
                if filename in {
                    candidate.get("filename"),
                    candidate.get("original_filename"),
                    (candidate.get("metadata") or {}).get("comfy_filename"),
                }:
                    cpath = active_store.get_asset_path(project_id, candidate["id"])
                    if cpath is not None:
                        return Response(
                            content=cpath.read_bytes(),
                            media_type=candidate["mime_type"],
                            headers={
                                "Content-Disposition": (
                                    f'{disposition}; filename="{candidate["original_filename"]}"'
                                )
                            },
                        )

        if filename:
            try:
                raw_bytes = await active_comfyui.view_output(
                    filename,
                    subfolder=subfolder,
                    storage_type=type,
                )
            except ComfyUIError as exc:
                raise HTTPException(
                    status_code=exc.status_code or 404,
                    detail=exc.message,
                ) from exc
            media_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/png"
            # El nombre viaja a Content-Disposition: sanear (sin comillas ni CR/LF).
            safe_name = sanitize_filename(os.path.basename(filename), fallback_ext=".bin")
            return Response(
                content=raw_bytes,
                media_type=media_type,
                headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
            )

        raise HTTPException(
            status_code=400,
            detail="Debe especificar 'project_id' + 'asset_id' o 'filename'.",
        )

    # --- Registro de plantillas (list / get / submit) ---

    @app.get("/studio/api/templates")
    async def list_templates_endpoint(
        request: Request,
        include_workflow: bool = Query(default=False),
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_cfg: StudioConfig = request.app.state.config
        # El registro HTTP expone SIEMPRE las 7 plantillas; cada entrada declara
        # `available`/`unavailable_reason` (gating por pesos 3D, VAL-UNREAL-004)
        # y es el panel quien no ofrece las no disponibles.
        return {
            "templates": active_registry.list_templates(
                include_workflow=include_workflow,
                three_d_available=active_cfg.three_d_weights_available(),
            )
        }

    @app.get("/studio/api/templates/{template_id}")
    async def get_template_endpoint(
        template_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_cfg: StudioConfig = request.app.state.config
        try:
            return active_registry.get_template(
                template_id,
                include_workflow=True,
                three_d_available=active_cfg.three_d_weights_available(),
            )
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/studio/api/templates/{template_id}/submit")
    @app.post("/studio/api/projects/{project_id}/templates/{template_id}/submit")
    async def submit_template_endpoint(
        template_id: str,
        request: Request,
        project_id: str | None = None,
    ):
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        active_redactor = active_cfg.redactor()

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Cuerpo JSON inválido: {exc}",
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422,
                detail="El cuerpo de la petición debe ser un objeto JSON.",
            )

        eff_project_id = project_id or payload.get("project_id")
        if "params" in payload:
            raw_params = payload.get("params")
        else:
            raw_params = {k: v for k, v in payload.items() if k != "project_id"}

        try:
            result = await active_registry.submit_render(
                template_id,
                raw_params,
                project_id=eff_project_id,
                store=active_store,
                comfyui_client=active_comfyui,
                broker=active_broker,
                strict=True,
                three_d_available=active_cfg.three_d_weights_available(),
            )
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TemplateParamError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=active_redactor.redact_value(
                    {
                        "detail": exc.message,
                        "errors": exc.errors,
                    }
                ),
            )
        except ComfyUIError as exc:
            raise HTTPException(
                status_code=exc.status_code or 502,
                detail=active_redactor.redact(exc.message),
            ) from exc

        return active_redactor.redact_value(result)

    @app.delete("/studio/api/projects/{project_id}/assets/{asset_id}")
    @app.delete("/studio/api/projects/{project_id}/media/{asset_id}")
    async def delete_project_asset(
        project_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        deleted = active_store.delete_asset(project_id, asset_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Medio no encontrado")
        return {"deleted": True, "id": asset_id}

    # --- Jobs y canal SSE de progreso ---

    @app.post("/studio/api/jobs", status_code=201)
    async def create_job(
        body: JobCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if body.project_id and active_store.get_project(body.project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        job = active_store.create_job(
            kind=body.kind,
            project_id=body.project_id,
            template_id=body.template_id,
            prompt_id=body.prompt_id,
            params=body.params,
        )
        active_broker.publish(
            "job",
            {
                "job_id": job["id"],
                "project_id": job["project_id"],
                "kind": job["kind"],
                "status": job["status"],
                "template_id": job["template_id"],
            },
        )
        return job

    @app.get("/studio/api/jobs")
    async def list_jobs(
        request: Request,
        project_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        return {
            "jobs": active_store.list_jobs(
                project_id=project_id,
                status=status,
            )
        }

    @app.get("/studio/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        job = active_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job no encontrado")
        return job

    @app.post("/studio/api/jobs/{job_id}/progress")
    async def report_job_progress(
        job_id: str,
        body: JobProgressRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        updated = active_store.update_job(
            job_id,
            status=body.status,
            prompt_id=body.prompt_id,
            progress_value=body.progress_value,
            progress_max=body.progress_max,
            current_node=body.current_node,
            result=body.result,
            error=body.error,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Job no encontrado")
        active_broker.publish(
            "progress",
            {
                "job_id": updated["id"],
                "project_id": updated["project_id"],
                "status": updated["status"],
                "value": updated["progress_value"],
                "max": updated["progress_max"],
                "node": updated["current_node"],
            },
        )
        active_broker.publish(
            "job",
            {
                "job_id": updated["id"],
                "project_id": updated["project_id"],
                "kind": updated["kind"],
                "status": updated["status"],
                "error": updated["error"],
            },
        )
        return updated

    @app.get("/studio/api/agent/tools")
    @app.get("/studio/api/tools")
    async def list_agent_tools(request: Request) -> dict[str, Any]:
        active_agent: StudioAgent = request.app.state.agent
        return {"tools": active_agent.tool_belt.to_openai_tools()}

    @app.post("/studio/api/projects/{project_id}/chat")
    async def project_chat_turn(
        project_id: str,
        body: ChatTurnRequest,
        request: Request,
    ):
        active_store: ProjectStore = request.app.state.store
        active_agent: StudioAgent = request.app.state.agent
        active_redactor = request.app.state.config.redactor()
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        wants_stream = body.stream or "text/event-stream" in (request.headers.get("accept") or "")
        if wants_stream:

            async def _sse_gen():
                try:
                    async for ev in active_agent.stream_turn(
                        project_id=project_id,
                        user_message=body.message,
                        asset_ids=body.asset_ids,
                        keyframe_data_urls=body.keyframe_data_urls,
                        max_iterations=body.max_iterations,
                    ):
                        yield format_sse_event(ev["type"], active_redactor.redact_value(ev))
                except Exception as exc:
                    logger.warning(
                        "Error no controlado en el stream SSE del turno de chat: %s",
                        active_redactor.redact(str(exc)),
                    )
                    err_ev = active_redactor.redact_value({"type": "error", "detail": str(exc)})
                    yield format_sse_event("error", err_ev)

            return StreamingResponse(
                _sse_gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            turn_result = await active_agent.run_turn(
                project_id=project_id,
                user_message=body.message,
                asset_ids=body.asset_ids,
                keyframe_data_urls=body.keyframe_data_urls,
                max_iterations=body.max_iterations,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=active_redactor.redact(str(exc)),
            ) from exc
        return active_redactor.redact_value(turn_result.to_dict())

    @app.get("/studio/api/projects/{project_id}/chat")
    async def get_project_chat_history(
        project_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_redactor = request.app.state.config.redactor()
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        payload = {
            "project_id": project_id,
            "messages": active_store.list_chat_messages(project_id, limit=limit),
        }
        return active_redactor.redact_value(payload)

    @app.delete("/studio/api/projects/{project_id}/chat")
    async def clear_project_chat_history(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        removed = active_store.clear_chat_messages(project_id)
        return {"deleted": True, "project_id": project_id, "removed_count": removed}

    @app.get("/studio/api/projects/{project_id}/keyframes")
    async def list_project_keyframes(
        project_id: str,
        request: Request,
        source_asset_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        kf_assets = active_store.list_assets(project_id, category="keyframes")
        items: list[dict[str, Any]] = []
        for asset in kf_assets:
            meta = dict(asset.get("metadata") or {})
            src_aid = meta.get("source_asset_id") or asset["id"]
            if source_asset_id and src_aid != source_asset_id:
                continue
            items.append(
                {
                    **asset,
                    "source_asset_id": src_aid,
                    "timestamp_sec": float(meta.get("timestamp_sec") or 0.0),
                    "frame_index": int(meta.get("frame_index") or 0),
                }
            )
        items.sort(
            key=lambda x: (str(x.get("source_asset_id")), float(x.get("timestamp_sec") or 0.0))
        )
        return {
            "project_id": project_id,
            "count": len(items),
            "keyframes": items,
        }

    @app.post("/studio/api/projects/{project_id}/keyframes/extract")
    async def extract_project_keyframes(
        project_id: str,
        body: KeyframeExtractRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        target_aid = body.asset_id
        if not target_aid:
            videos = active_store.list_assets(project_id, kind="video")
            if not videos:
                raise HTTPException(
                    status_code=400,
                    detail="El proyecto no contiene ningún vídeo para extraer keyframes.",
                )
            target_aid = videos[0]["id"]
        try:
            kfs = await run_in_threadpool(
                extract_video_keyframes,
                active_store,
                project_id,
                target_aid,
                interval_sec=body.interval_sec,
                scene_threshold=body.scene_threshold,
                max_frames=body.max_frames,
                ffmpeg_timeout_s=request.app.state.config.studio_ffmpeg_timeout_s,
                ffprobe_timeout_s=request.app.state.config.studio_ffprobe_timeout_s,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "extracted",
            "project_id": project_id,
            "source_asset_id": target_aid,
            "count": len(kfs),
            "keyframes": kfs,
        }

    @app.post("/studio/api/projects/{project_id}/perception/index")
    async def index_project_perception(
        project_id: str,
        body: PerceptionIndexRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        # M3.3: índice stale ⇒ escrituras rechazan con 409 hasta reindexar.
        if request.app.state.perception_stale:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Índice de percepción desactualizado (dims almacenadas no coinciden "
                    "con las activas). Ejecuta POST /studio/api/perception/reindex antes "
                    "de escribir nuevos embeddings."
                ),
            )

        # Trabajo síncrono pesado (ffmpeg/embeddings/sqlite): fuera del event loop para
        # no bloquear /health, el SSE de progreso ni el resto de peticiones.
        def _index_sync() -> dict[str, Any]:
            if body.asset_id:
                res = active_clip.index_asset(
                    project_id,
                    body.asset_id,
                    interval_sec=body.interval_sec,
                )
                faces_count = 0
                if body.detect_faces:
                    faces_count += len(
                        active_face.detect_and_index_asset(project_id, body.asset_id)
                    )
                    for shot in res.get("shots", []):
                        kf_id = shot.get("keyframe_asset_id")
                        if kf_id and kf_id != body.asset_id:
                            faces_count += len(
                                active_face.detect_and_index_asset(
                                    project_id,
                                    kf_id,
                                    source_asset_id=body.asset_id,
                                    timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                                )
                            )
                return {
                    **res,
                    "faces_detected": faces_count,
                    "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
                }
            res = active_clip.index_project_media(
                project_id,
                interval_sec=body.interval_sec,
            )
            faces_count = 0
            if body.detect_faces:
                for item in res.get("results", []):
                    src_aid = item["asset_id"]
                    for shot in item.get("shots", []):
                        kf_id = shot.get("keyframe_asset_id") or src_aid
                        faces_count += len(
                            active_face.detect_and_index_asset(
                                project_id,
                                kf_id,
                                source_asset_id=src_aid,
                                timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                            )
                        )
            return {
                **res,
                "faces_detected": faces_count,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }

        try:
            return await run_in_threadpool(_index_sync)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/faces/index")
    async def index_project_faces_endpoint(
        project_id: str,
        request: Request,
        body: FaceIndexRequest | None = None,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        req_body = body or FaceIndexRequest()
        try:
            registered_identity = None
            if req_body.name:
                registered_identity = active_face.register_identity(
                    project_id,
                    name=req_body.name,
                    reference_asset_id=req_body.reference_asset_id,
                )
            res = await run_in_threadpool(
                active_face.index_project_faces,
                project_id,
                keyframe_asset_ids=req_body.keyframe_asset_ids,
            )
            return {
                **res,
                "identity": registered_identity,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/perception/search")
    async def search_project_perception_get(
        project_id: str,
        request: Request,
        query: str | None = Query(default=None),
        asset_id: str | None = Query(default=None),
        identity_id: str | None = Query(default=None),
        identity_name: str | None = Query(default=None),
        reference_asset_id: str | None = Query(default=None),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        # M3.3: índice stale ⇒ lecturas devuelven vacío con stale=true, nunca ranking erróneo.
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "text",
                "query": query or "",
                "count": 0,
                "shots": [],
                "stale": True,
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }

        if identity_id or identity_name or reference_asset_id:
            try:
                shots = active_face.find_shots_face(
                    project_id,
                    identity_id=identity_id,
                    identity_name=identity_name,
                    reference_asset_id=reference_asset_id,
                    text_query=query,
                    limit=limit,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "project_id": project_id,
                "mode": "combined" if query else "face",
                "query": query,
                "identity_id": identity_id,
                "identity_name": identity_name,
                "count": len(shots),
                "shots": shots,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
            }

        shots = active_clip.find_shots_text(
            project_id,
            query or "",
            limit=limit,
            asset_id=asset_id,
        )
        return {
            "project_id": project_id,
            "mode": "text",
            "query": query or "",
            "count": len(shots),
            "shots": shots,
            "simulated": True,
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    @app.post("/studio/api/projects/{project_id}/perception/search")
    async def search_project_perception_post(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        eff_query = body.query if body.query is not None else body.text_query
        eff_name = body.identity_name or body.person_name
        return await search_project_perception_get(
            project_id=project_id,
            request=request,
            query=eff_query,
            asset_id=body.asset_id,
            identity_id=body.identity_id,
            identity_name=eff_name,
            reference_asset_id=body.reference_asset_id,
            limit=body.limit,
        )

    @app.post("/studio/api/projects/{project_id}/search/faces")
    async def search_project_faces_endpoint(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "face",
                "identity_id": body.identity_id,
                "identity_name": body.identity_name or body.person_name,
                "count": 0,
                "shots": [],
                "stale": True,
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }
        eff_name = body.identity_name or body.person_name
        try:
            shots = active_face.find_shots_face(
                project_id,
                identity_id=body.identity_id,
                identity_name=eff_name,
                reference_asset_id=body.reference_asset_id,
                limit=body.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "mode": "face",
            "identity_id": body.identity_id,
            "identity_name": eff_name,
            "count": len(shots),
            "shots": shots,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            "simulated": True,
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    @app.post("/studio/api/projects/{project_id}/search/combined")
    async def search_project_combined_endpoint(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "combined",
                "query": body.query if body.query is not None else body.text_query,
                "identity_id": body.identity_id,
                "identity_name": body.identity_name or body.person_name,
                "count": 0,
                "shots": [],
                "stale": True,
                "simulated": True,
                "simulation_notice": SIMULATED_ML_NOTICE,
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }
        eff_query = body.query if body.query is not None else body.text_query
        eff_name = body.identity_name or body.person_name
        try:
            shots = active_face.combined_search(
                project_id,
                identity_id=body.identity_id,
                identity_name=eff_name,
                reference_asset_id=body.reference_asset_id,
                text_query=eff_query,
                limit=body.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "mode": "combined",
            "query": eff_query,
            "identity_id": body.identity_id,
            "identity_name": eff_name,
            "count": len(shots),
            "shots": shots,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            "simulated": True,
            "simulation_notice": SIMULATED_ML_NOTICE,
        }

    @app.post("/studio/api/perception/reindex")
    async def perception_reindex(request: Request) -> dict[str, Any]:
        """Reconstruye los índices vectoriales con las dims activas (M3.3).

        - Elimina y recrea `vec_clip_shots` / `vec_face_identities` / `vec_face_detections`
          con las dims del backend activo (768/512).
        - Re-embede desde los keyframes PNG existentes en `clip_shots` /
          `face_detections` (los PNG y `studio.db` no se tocan).
        - Reescribe `schema_meta` con `clip_dim`, `face_dim`, extractores y `built_at`.
        - Solo se permite cuando el índice está `stale`; si no hay desajuste, devuelve
          `already_current` sin hacer nada.
        """
        _require_perception(request)
        active_store: ProjectStore = request.app.state.store
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        vs: PerceptionVectorStore = request.app.state.vector_store
        if vs is None or active_clip is None or active_face is None:
            raise HTTPException(
                status_code=503,
                detail="Percepción vectorial no disponible para reindexar.",
            )

        stored_clip, stored_face = vs.stored_dims()
        if (
            stored_clip == CLIP_EMBED_DIM
            and stored_face == FACE_EMBED_DIM
            and stored_clip is not None
        ):
            return {
                "status": "already_current",
                "clip_dim": CLIP_EMBED_DIM,
                "face_dim": FACE_EMBED_DIM,
                "message": "El índice ya usa las dims activas; no es necesario reindexar.",
            }

        # Ejecutar reindex síncrono fuera del event loop (pesado: sqlite + embeddings).
        def _reindex_sync() -> dict[str, Any]:
            with vs._connection() as conn:
                # 1. Dropear vec tables
                conn.execute("DROP TABLE IF EXISTS vec_clip_shots")
                conn.execute("DROP TABLE IF EXISTS vec_face_identities")
                conn.execute("DROP TABLE IF EXISTS vec_face_detections")
                # 2. Recrear con dims activas
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_clip_shots "
                    f"USING vec0(embedding float[{CLIP_EMBED_DIM}])"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_face_identities "
                    f"USING vec0(embedding float[{FACE_EMBED_DIM}])"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_face_detections "
                    f"USING vec0(embedding float[{FACE_EMBED_DIM}])"
                )
                # 3. Borrar shots/identities/detecciones existentes (se reconstruyen)
                conn.execute("DELETE FROM clip_shots")
                conn.execute("DELETE FROM face_identities")
                conn.execute("DELETE FROM face_detections")
                # 4. Reescribir schema_meta
                from datetime import UTC, datetime
                now = datetime.now(UTC).isoformat()
                vs.set_schema_meta("clip_dim", str(CLIP_EMBED_DIM))
                vs.set_schema_meta("face_dim", str(FACE_EMBED_DIM))
                vs.set_schema_meta("clip_extractor", "openai/clip-vit-large-patch14")
                vs.set_schema_meta("face_extractor", "buffalo_l/w600k_r50")
                vs.set_schema_meta("built_at", now)
                conn.commit()

            # 5. Re-indexar todos los keyframes existentes del proyecto (por defecto el primero)
            projects = active_store.list_projects()
            if not projects:
                return {
                    "status": "done",
                    "clip_dim": CLIP_EMBED_DIM,
                    "face_dim": FACE_EMBED_DIM,
                    "projects_reindexed": 0,
                    "message": "No hay proyectos; índice recreado vacío.",
                }
            target_project = projects[0]["id"]
            res = active_clip.index_project_media(target_project)
            return {
                "status": "done",
                "clip_dim": CLIP_EMBED_DIM,
                "face_dim": FACE_EMBED_DIM,
                "project_id": target_project,
                "shots_indexed": res.get("indexed_shots_count", 0),
                "skipped_synthetic": res.get("skipped_synthetic_count", 0),
                "message": "Índice reconstruido con dims activas.",
            }

        import asyncio

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _reindex_sync)
        # Marcar como no stale tras reindex exitoso
        request.app.state.perception_stale = False
        return result

    @app.get("/studio/api/projects/{project_id}/identities")
    async def list_project_identities(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        items = active_store.list_identities(project_id)
        return {
            "project_id": project_id,
            "count": len(items),
            "identities": items,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
        }

    @app.post("/studio/api/projects/{project_id}/identities", status_code=201)
    async def register_project_identity(
        project_id: str,
        body: IdentityCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            res = active_face.register_identity(
                project_id,
                name=body.name,
                reference_asset_id=body.reference_asset_id,
            )
            return {
                **res,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/studio/api/projects/{project_id}/identities/{identity_id}")
    async def rename_project_identity(
        project_id: str,
        identity_id: str,
        body: IdentityRenameRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            updated = active_face.rename_identity(project_id, identity_id, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Identidad no encontrada")
        return updated

    @app.delete("/studio/api/projects/{project_id}/identities/{identity_id}")
    async def delete_project_identity(
        project_id: str,
        identity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        deleted = active_store.delete_identity(project_id, identity_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Identidad no encontrada")
        return {"deleted": True, "id": identity_id}

    @app.get("/studio/api/projects/{project_id}/transcriptions")
    async def list_project_transcriptions(
        project_id: str,
        request: Request,
        asset_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        items = active_store.list_transcriptions(project_id, asset_id=asset_id)
        latest = items[0] if items else None
        return {
            "project_id": project_id,
            "count": len(items),
            "latest": latest,
            "transcriptions": items,
        }

    @app.get("/studio/api/projects/{project_id}/transcriptions/{transcription_id}/subtitles")
    async def export_project_subtitles(
        project_id: str,
        transcription_id: str,
        request: Request,
        fmt: str = Query(default="srt", pattern="^(srt|vtt)$"),
        include_speakers: bool = Query(default=True),
        download: bool = Query(default=False),
    ) -> Response:
        """Exporta la transcripción como SRT o WebVTT (M2, `M3-IDEA-032`).

        Es formateo puro sobre datos ya persistidos: funciona igual con la transcripción
        simulada actual y con la real del hito M3.
        """
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        transcription = active_store.get_transcription(project_id, transcription_id)
        if transcription is None:
            raise HTTPException(status_code=404, detail="Transcripción no encontrada")
        try:
            body = render_subtitles(
                transcription,
                fmt=fmt,  # type: ignore[arg-type]
                include_speakers=include_speakers,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media_type = "application/x-subrip" if fmt == "srt" else "text/vtt"
        headers = {"Cache-Control": "no-store"}
        if download:
            filename = subtitle_filename(transcription, fmt=fmt)  # type: ignore[arg-type]
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return Response(
            content=body,
            media_type=f"{media_type}; charset=utf-8",
            headers=headers,
        )

    @app.post("/studio/api/projects/{project_id}/transcribe")
    async def transcribe_project_endpoint(
        project_id: str,
        body: TranscribeRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            result = await run_in_threadpool(
                transcribe_project_asset,
                active_store,
                project_id,
                asset_id=body.asset_id,
                language=body.language,
                script_segments=body.script_segments,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish_transcription_event(active_broker, project_id, result)
        return {
            "status": "transcribed",
            "project_id": project_id,
            "transcription": result,
        }

    @app.post("/studio/api/projects/{project_id}/diarize")
    async def diarize_project_endpoint(
        project_id: str,
        body: DiarizeRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            result = await run_in_threadpool(
                diarize_project_asset,
                active_store,
                project_id,
                asset_id=body.asset_id,
                transcription_id=body.transcription_id,
                expected_speakers=body.expected_speakers,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(result.get("transcription"), dict):
            _publish_transcription_event(active_broker, project_id, result["transcription"])
        return {
            "status": "diarized",
            "project_id": project_id,
            **result,
        }

    @app.get("/studio/api/projects/{project_id}/edl")
    async def list_project_edls(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        latest = active_store.get_latest_edl(project_id)
        return {
            "project_id": project_id,
            "versions": active_store.list_edl_versions(project_id),
            "latest": latest,
            "edl": latest,
        }

    @app.post("/studio/api/projects/{project_id}/edl", status_code=201)
    async def save_project_edl_endpoint(
        project_id: str,
        body: EdlCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        cuts_dicts = [c.model_dump(exclude_none=True) for c in body.cuts]
        total_dur = round(sum(c.out_sec - c.in_sec for c in body.cuts), 3)
        edl_doc: dict[str, Any] = {
            "schema": "comfy_studio.edl.v1",
            "title": body.title,
            "aspect_ratio": body.aspect_ratio,
            "source_asset_id": body.source_asset_id,
            "total_duration_sec": total_dur,
            "cuts": cuts_dicts,
            "notes": body.notes,
        }
        saved = active_store.save_edl_version(project_id, edl=edl_doc)
        active_broker.publish(
            "edl",
            {
                "project_id": project_id,
                "edl_id": saved["id"],
                "version": saved["version"],
                "aspect_ratio": body.aspect_ratio,
                "total_duration_sec": total_dur,
                "cuts_count": len(cuts_dicts),
                # M2: `status` distingue propuesta de render en el evento webhook.
                "status": "proposed",
            },
        )
        return saved

    @app.post("/studio/api/projects/{project_id}/edl/highlights")
    async def generate_project_edl_highlights_endpoint(
        project_id: str,
        body: EdlHighlightsRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            saved = await run_in_threadpool(
                generate_highlights_edl,
                active_store,
                project_id,
                asset_id=body.asset_id,
                transcription_id=body.transcription_id,
                title=body.title,
                aspect_ratio=body.aspect_ratio,  # type: ignore[arg-type]
                remove_fillers=body.remove_fillers,
                min_pause_sec=body.min_pause_sec,
                max_duration_sec=body.max_duration_sec,
                speaker_filter=body.speaker_filter,
                hybrid=body.hybrid,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        active_broker.publish(
            "edl",
            {
                "project_id": project_id,
                "edl_id": saved["id"],
                "version": saved["version"],
                "aspect_ratio": body.aspect_ratio,
                "total_duration_sec": saved["edl"].get("total_duration_sec", 0.0),
                "cuts_count": len(saved["edl"].get("cuts") or []),
                "status": "proposed",
            },
        )
        return saved

    @app.post("/studio/api/projects/{project_id}/edl/build")
    async def build_project_edl_endpoint(
        project_id: str,
        body: EdlBuildRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        cuts_dicts = (
            [c.model_dump(exclude_none=True) for c in body.cuts] if body.cuts is not None else None
        )

        async def _render_one(aspect: str | None) -> dict[str, Any]:
            try:
                result = await run_in_threadpool(
                    execute_edl_ffmpeg,
                    active_store,
                    project_id,
                    edl_id=body.edl_id,
                    version=body.version,
                    aspect_ratio=aspect,
                    source_asset_id=body.source_asset_id,
                    cuts=cuts_dicts,
                    fps=body.fps,
                    ffmpeg_timeout_s=active_cfg.studio_ffmpeg_timeout_s,
                    ffprobe_timeout_s=active_cfg.studio_ffprobe_timeout_s,
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Un evento webhook/SSE por variante (M2, `M3-IDEA-036`).
            active_broker.publish(
                "edl",
                {
                    "project_id": project_id,
                    "edl_id": result["edl_id"],
                    "version": result["version"],
                    "aspect_ratio": result["aspect_ratio"],
                    "output_asset_id": result["output_asset"]["id"],
                    "status": "built",
                },
            )
            return result

        variants = body.resolved_aspect_ratios()
        if not variants:
            return await _render_one(body.aspect_ratio)

        # Render por lotes: secuencial a propósito. Cada EDL ya lanza decenas de
        # subprocess ffmpeg; paralelizarlos competiría por CPU con ComfyUI y haría
        # impredecible el tiempo total. El resultado expone una entrada por variante.
        results: list[dict[str, Any]] = []
        for aspect in variants:
            results.append(await _render_one(aspect))
        first = results[0]
        return {
            **first,
            "variants": results,
            "variant_count": len(results),
            "aspect_ratios": [r["aspect_ratio"] for r in results],
        }

    @app.post("/studio/api/projects/{project_id}/edl/{version}/render")
    async def render_project_edl_version_endpoint(
        project_id: str,
        version: int,
        request: Request,
        body: EdlBuildRequest | None = None,
    ) -> dict[str, Any]:
        req_body = body or EdlBuildRequest()
        req_body.version = version
        return await build_project_edl_endpoint(
            project_id=project_id,
            body=req_body,
            request=request,
        )

    @app.get("/studio/api/progress")
    async def sse_progress(
        request: Request,
        heartbeat_interval: float | None = Query(default=None, ge=0.01, le=300.0),
        replay: bool = Query(default=False),
        max_events: int | None = Query(default=None, ge=1, le=1000),
    ) -> StreamingResponse:
        active_cfg: StudioConfig = request.app.state.config
        active_broker: ProgressBroker = request.app.state.broker
        interval = (
            heartbeat_interval
            if heartbeat_interval is not None
            else active_cfg.sse_heartbeat_seconds
        )
        return StreamingResponse(
            active_broker.stream(
                heartbeat_interval=interval,
                replay=replay,
                max_events=max_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.api_route(
        "/studio/api/{unknown_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unknown_api_route(unknown_path: str) -> JSONResponse:
        raise HTTPException(
            status_code=404,
            detail=f"Ruta de API no encontrada: /studio/api/{unknown_path}",
        )

    panel_dir = _resolve_panel_dir()
    if panel_dir is not None:
        index_file = panel_dir / "index.html"

        @app.get("/studio", include_in_schema=False)
        async def serve_studio_root_no_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        @app.get("/studio/", include_in_schema=False)
        async def serve_studio_root_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        app.mount(
            "/studio",
            StaticFiles(directory=str(panel_dir), html=True),
            name="studio-panel",
        )

    return app


def _resolve_panel_dir() -> Path | None:
    """Localiza el directorio estático panel/ en entorno local o contenedor."""
    candidates: list[Path] = []
    env_dir = os.environ.get("STUDIO_PANEL_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            Path(__file__).resolve().parents[2] / "panel",
            Path("/etc/comfyui/panel"),
            Path("/opt/studio/panel"),
            Path.cwd() / "panel",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


app = create_app()
