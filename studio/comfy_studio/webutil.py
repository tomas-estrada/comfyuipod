"""Helpers, modelos de petición y utilidades HTTP compartidas de Comfy Studio.

Extraído de `app.py` (M7-SCR-002): las rutas viven en `comfy_studio/routers/` y
`create_app` en `app.py`; este módulo alberga lo que ambos usan.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
import shutil
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, Headers
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser

from comfy_studio.agent.tools import EdlCutItem
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import (
    StudioConfig,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.templates import (
    TemplateRegistry,
)

logger = logging.getLogger(__name__)


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
    project_id: str | None = Field(
        default=None,
        description=(
            "Proyecto de la galería donde se registran las salidas de la "
            "producción (shots aceptados + reel final) con linaje."
        ),
    )


class ProductionShotEdit(BaseModel):
    """Edición de un shot del plan (solo mientras la producción espera aprobación)."""

    id: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    duration_s: float | None = Field(default=None, ge=1.0, le=120.0)
    remove: bool = Field(default=False)


class ProductionPlanUpdateRequest(BaseModel):
    """Cuerpo para editar el plan de una producción en `awaiting_approval`."""

    idea: str | None = Field(default=None, min_length=1, max_length=4000)
    shots: list[ProductionShotEdit] | None = Field(default=None, max_length=200)


class ReviewPinCreateRequest(BaseModel):
    """Payload de creación de un review pin anclado a una versión de EDL (M3-RVP-001)."""

    model_config = ConfigDict(extra="forbid")

    edl_version: int = Field(..., ge=1, description="Versión del EDL revisado.")
    t_start: float = Field(
        ..., ge=0.0, description="Segundo de inicio del pin en el timeline renderizado."
    )
    t_end: float = Field(
        ..., ge=0.0, description="Segundo de fin del pin (>= t_start)."
    )
    severity: Literal["note", "issue", "blocker"] = Field(
        default="note", description="Severidad del feedback."
    )
    body: str = Field(
        ..., min_length=1, max_length=2000, description="Comentario del revisor."
    )

    @model_validator(mode="after")
    def _check_window(self) -> ReviewPinCreateRequest:
        if self.t_end < self.t_start:
            raise ValueError(
                f"t_end ({self.t_end}) no puede ser menor que t_start ({self.t_start})."
            )
        return self


class ReviewPinUpdateRequest(BaseModel):
    """Payload de actualización de un review pin (M3-RVP-001)."""

    model_config = ConfigDict(extra="forbid")

    t_start: float | None = Field(default=None, ge=0.0)
    t_end: float | None = Field(default=None, ge=0.0)
    status: Literal["open", "resolved", "discarded"] | None = None
    resolution_note: str | None = Field(
        default=None, max_length=1000,
        description="Nota opcional de resolución; solo aplica con status != open."
    )


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
        max_files: float = 1000,
        max_fields: float = 1000,
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
        max_files: float = 1000,
        max_fields: float = 1000,
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


#: Margen de seguridad en el volumen de datos antes de aceptar una subida:
#: cubre WAL de SQLite, spools temporales y escrituras concurrentes de ffmpeg.
_DISK_SAFETY_MARGIN_BYTES = 1024**3  # 1 GiB


def _check_upload_free_disk(
    headers: Headers,
    data_dir: Path,
    margin_bytes: int = _DISK_SAFETY_MARGIN_BYTES,
) -> None:
    """507 si el volumen de datos no tiene sitio para el cuerpo declarado.

    nginx tiene `client_max_body_size 0`, así que el límite de bytes lo pone la app;
    este guard añade el límite de *espacio libre real* para que una subida grande
    no llene el volumen que comparten studio.db, index.db y el staging de la Fase 3.
    Sin cabecera Content-Length (chunked) no se puede estimar: se omite aquí y el
    parser acotado por stream sigue aplicando los techos por archivo y totales.
    """
    raw_cl = headers.get("Content-Length", "").strip()
    try:
        content_length = int(raw_cl) if raw_cl else 0
    except ValueError:
        return
    if content_length <= 0:
        return
    try:
        free = shutil.disk_usage(data_dir).free
    except OSError:
        return
    if free < content_length + margin_bytes:
        raise HTTPException(
            status_code=507,
            detail=(
                "Espacio insuficiente en el volumen de datos: "
                f"libres={free} bytes, la subida declara {content_length} bytes "
                f"(+ margen {margin_bytes}). Libera espacio o divide la subida."
            ),
        )


class _BoundedUploadAPIRoute(APIRoute):
    """APIRoute que aplica recepción acotada antes de que FastAPI resuelva request.form()."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_route_handler = super().get_route_handler()

        async def custom_route_handler(request: Request) -> Response:
            if _is_media_upload_request(request):
                active_cfg: StudioConfig = request.app.state.config
                limit = active_cfg.max_upload_bytes
                _check_upload_content_length(request.headers, limit)
                _check_upload_free_disk(request.headers, active_cfg.studio_data_dir)
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


async def _run_render_job_task(app: FastAPI, job_id: str) -> None:
    """Ejecuta en segundo plano un job render encolado por el endpoint de submit (M7-SCR-001).

    Con semáforo acotado (`STUDIO_MAX_CONCURRENT_RENDERS`); ComfyUI serializa la
    ejecución real. Si el servidor se apaga a mitad, el job queda en estado en
    vuelo y `recovery.py` lo marca `interrupted` (y lo reanuda una vez) al arrancar.
    """
    registry: TemplateRegistry = app.state.template_registry
    store: ProjectStore = app.state.store
    client: ComfyUIClient = app.state.comfyui_client
    broker: ProgressBroker = app.state.broker
    try:
        async with app.state.render_semaphore:
            await registry.execute_render_job(
                job_id, store=store, comfyui_client=client, broker=broker
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Render en segundo plano del job %s falló", job_id)


