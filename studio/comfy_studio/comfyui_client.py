"""Cliente HTTP y parser de eventos WebSocket para ComfyUI (127.0.0.1:18188 / stub 39172)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from comfy_studio.config import (
    DEFAULT_MAX_UPLOAD_BYTES,
    DEFAULT_STUDIO_COMFYUI_URL,
    SecretRedactor,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

logger = logging.getLogger("comfy_studio.comfyui_client")

_DEFAULT_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class ComfyUIError(Exception):
    """Error base del cliente ComfyUI (nunca silencioso)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        prompt_id: str | None = None,
        node_errors: dict[str, Any] | None = None,
        details: Any = None,
        retriable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.prompt_id = prompt_id
        self.node_errors = node_errors or {}
        self.details = details
        self.retriable = retriable


class ComfyUITimeoutError(ComfyUIError):
    """Error de timeout en petición HTTP o sondeo de /history/{prompt_id}."""


class ComfyUIConnectionError(ComfyUIError):
    """Error de transporte o conexión hacia ComfyUI tras agotar reintentos."""


class ComfyUIExecutionError(ComfyUIError):
    """Error de validación de grafo (400 / node_errors) o fallo de ejecución en nodo."""


class PromptSubmission(BaseModel):
    """Respuesta de POST /prompt."""

    prompt_id: str
    number: int = 0
    node_errors: dict[str, Any] = Field(default_factory=dict)

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


class QueueStatus(BaseModel):
    """Estado de la cola devuelto por GET /queue."""

    queue_running: list[list[Any]] = Field(default_factory=list)
    queue_pending: list[list[Any]] = Field(default_factory=list)

    def running_prompt_ids(self) -> list[str]:
        return [
            str(entry[1])
            for entry in self.queue_running
            if isinstance(entry, list) and len(entry) > 1
        ]

    def pending_prompt_ids(self) -> list[str]:
        return [
            str(entry[1])
            for entry in self.queue_pending
            if isinstance(entry, list) and len(entry) > 1
        ]

    def is_prompt_active(self, prompt_id: str) -> bool:
        return prompt_id in self.running_prompt_ids() or (
            prompt_id in self.pending_prompt_ids()
        )


class ComfyUIOutputFile(BaseModel):
    """Archivo de salida registrado en /history/{prompt_id}."""

    node_id: str
    kind: str  # images | gifs | video | audio
    filename: str
    subfolder: str = ""
    storage_type: str = "output"
    format: str | None = None


class PromptResult(BaseModel):
    """Resultado consolidado de un prompt ejecutado en ComfyUI."""

    prompt_id: str
    status_str: str = "success"
    completed: bool = True
    outputs: list[ComfyUIOutputFile] = Field(default_factory=list)
    raw_history: dict[str, Any] = Field(default_factory=dict)


class ComfyUIUploadResult(BaseModel):
    """Respuesta de POST /upload/image o subida de vídeo VHS."""

    name: str
    subfolder: str = ""
    storage_type: str = "input"
    overwrite: bool = True

    @property
    def relative_path(self) -> str:
        clean_sub = self.subfolder.strip("/\\")
        if clean_sub:
            return f"{clean_sub}/{self.name}"
        return self.name


class ComfyUIWsEvent(BaseModel):
    """Evento estructurado parseado del canal /ws de ComfyUI."""

    event_type: str
    prompt_id: str | None = None
    node: str | None = None
    value: float | None = None
    max: float | None = None
    queue_remaining: int | None = None
    output: dict[str, Any] | None = None
    error_message: str | None = None
    raw_data: dict[str, Any] = Field(default_factory=dict)


def parse_ws_event(raw_message: str | bytes | dict[str, Any]) -> ComfyUIWsEvent | None:
    """Parsea un mensaje de /ws de ComfyUI; ignora frames binarios de preview."""
    if isinstance(raw_message, bytes):
        try:
            text = raw_message.decode("utf-8")
        except UnicodeDecodeError:
            return None
        raw_message = text

    if isinstance(raw_message, str):
        try:
            payload = json.loads(raw_message)
        except json.JSONDecodeError:
            return None
    elif isinstance(raw_message, dict):
        payload = raw_message
    else:
        return None

    if not isinstance(payload, dict):
        return None

    event_type = str(payload.get("type") or "").strip()
    if not event_type:
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}

    prompt_id = data.get("prompt_id")
    node = data.get("node")
    value: float | None = None
    max_val: float | None = None
    queue_remaining: int | None = None
    output: dict[str, Any] | None = None
    error_message: str | None = None

    if event_type == "progress":
        if data.get("value") is not None:
            value = float(data["value"])
        if data.get("max") is not None:
            max_val = float(data["max"])
    elif event_type == "status":
        status_obj = data.get("status")
        if isinstance(status_obj, dict):
            exec_info = status_obj.get("exec_info")
            if isinstance(exec_info, dict) and exec_info.get("queue_remaining") is not None:
                queue_remaining = int(exec_info["queue_remaining"])
    elif event_type == "executed":
        if isinstance(data.get("output"), dict):
            output = data["output"]
    elif event_type in {"execution_error", "execution_interrupted"}:
        error_message = str(
            data.get("exception_message")
            or data.get("message")
            or f"ComfyUI {event_type}"
        )
        if node is None and data.get("node_id") is not None:
            node = str(data["node_id"])

    return ComfyUIWsEvent(
        event_type=event_type,
        prompt_id=str(prompt_id) if prompt_id is not None else None,
        node=str(node) if node is not None else None,
        value=value,
        max=max_val,
        queue_remaining=queue_remaining,
        output=output,
        error_message=error_message,
        raw_data=data,
    )


def extract_outputs(history_entry: dict[str, Any]) -> list[ComfyUIOutputFile]:
    """Extrae artefactos (images, gifs, video, videos, audio) de una entrada de /history."""
    outputs_by_node = history_entry.get("outputs")
    if not isinstance(outputs_by_node, dict):
        return []

    files: list[ComfyUIOutputFile] = []
    for node_id, node_out in outputs_by_node.items():
        if not isinstance(node_out, dict):
            continue
        for kind in ("gifs", "video", "videos", "images", "audio"):
            items = node_out.get(kind)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                if not filename:
                    continue
                files.append(
                    ComfyUIOutputFile(
                        node_id=str(node_id),
                        kind=kind,
                        filename=str(filename),
                        subfolder=str(item.get("subfolder") or ""),
                        storage_type=str(item.get("type") or "output"),
                        format=item.get("format"),
                    )
                )
    return files


def _extract_error_message_from_body(body: Any, default: str) -> tuple[str, dict[str, Any]]:
    """Extrae mensaje legible y node_errors de un cuerpo de error de ComfyUI o FastAPI."""
    node_errors: dict[str, Any] = {}
    if not isinstance(body, dict):
        return default, node_errors

    # FastAPI HTTPException envuelve en "detail"
    target = body.get("detail") if isinstance(body.get("detail"), dict) else body
    if isinstance(body.get("detail"), str):
        return body["detail"], node_errors

    if isinstance(target.get("node_errors"), dict):
        node_errors = target["node_errors"]

    parts: list[str] = []
    err_field = target.get("error")
    if isinstance(err_field, dict):
        msg = err_field.get("message") or err_field.get("type")
        if msg:
            parts.append(str(msg))
    elif isinstance(err_field, str) and err_field:
        parts.append(err_field)

    for node_id, ninfo in node_errors.items():
        if isinstance(ninfo, dict):
            errs = ninfo.get("errors")
            if isinstance(errs, list):
                for err_item in errs:
                    if isinstance(err_item, dict) and err_item.get("message"):
                        parts.append(f"node {node_id}: {err_item['message']}")
        elif isinstance(ninfo, str):
            parts.append(f"node {node_id}: {ninfo}")

    return ("; ".join(parts) if parts else default), node_errors


class ComfyUIClient:
    """Cliente HTTP/WS hacia ComfyUI con reintentos, backoff y elevación de errores al job."""

    def __init__(
        self,
        base_url: str = DEFAULT_STUDIO_COMFYUI_URL,
        *,
        client_id: str = "comfy-studio",
        timeout: float = 30.0,
        connect_timeout: float = 5.0,
        poll_interval: float = 0.5,
        poll_timeout: float = 300.0,
        max_retries: int = 3,
        backoff_base: float = 0.2,
        backoff_factor: float = 2.0,
        backoff_max: float = 5.0,
        retry_status_codes: set[int] | frozenset[int] = _DEFAULT_RETRY_STATUS_CODES,
        transport: httpx.AsyncBaseTransport | None = None,
        store: ProjectStore | None = None,
        broker: ProgressBroker | None = None,
        redactor: SecretRedactor | None = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = float(backoff_base)
        self.backoff_factor = float(backoff_factor)
        self.backoff_max = float(backoff_max)
        self.retry_status_codes = frozenset(retry_status_codes)
        self._transport = transport
        self._store = store
        self._broker = broker
        self._redactor = redactor or SecretRedactor()
        self._sleep = sleep_func

    def _redact(self, text: str) -> str:
        return self._redactor.redact(text)

    def _compute_backoff(self, attempt: int) -> float:
        delay = self.backoff_base * (self.backoff_factor**attempt)
        return min(delay, self.backoff_max)

    def _fail_job(
        self,
        error: ComfyUIError,
        *,
        job_id: str | None = None,
        project_id: str | None = None,
        prompt_id: str | None = None,
    ) -> None:
        redacted_msg = self._redact(error.message)
        error.message = redacted_msg
        error.args = (redacted_msg,)
        if job_id and self._store is not None:
            self._store.update_job(
                job_id,
                status="error",
                prompt_id=prompt_id or error.prompt_id,
                error=redacted_msg,
                result={
                    "error": redacted_msg,
                    "status_code": error.status_code,
                    "node_errors": self._redactor.redact_value(error.node_errors),
                },
            )
        if self._broker is not None:
            self._broker.publish(
                "job",
                {
                    "job_id": job_id,
                    "project_id": project_id,
                    "prompt_id": prompt_id or error.prompt_id,
                    "status": "error",
                    "error": redacted_msg,
                },
            )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        req_timeout = httpx.Timeout(
            timeout if timeout is not None else self.timeout,
            connect=min(
                self.connect_timeout,
                timeout if timeout is not None else self.timeout,
            ),
        )

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    transport=self._transport,
                    timeout=req_timeout,
                ) as client:
                    response = await client.request(
                        method,
                        url,
                        json=json_body,
                        params=params,
                        files=files,
                        data=data,
                    )

                if response.status_code < 400:
                    return response

                # Comprobar si el código de estado HTTP es transitorio y quedan reintentos
                if (
                    response.status_code in self.retry_status_codes
                    and attempt < self.max_retries
                ):
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        "ComfyUI %s %s devolvió %d (intento %d/%d); reintentando en %.2fs",
                        method,
                        path,
                        response.status_code,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    await self._sleep(delay)
                    continue

                # Error HTTP definitivo: parsear detalle y elevar
                parsed_body: Any = None
                try:
                    parsed_body = response.json()
                except Exception:
                    parsed_body = response.text

                err_msg, node_errors = _extract_error_message_from_body(
                    parsed_body,
                    f"ComfyUI HTTP {response.status_code} en {method} {path}",
                )
                redacted = self._redact(err_msg)
                if response.status_code == 400 or node_errors:
                    raise ComfyUIExecutionError(
                        redacted,
                        status_code=response.status_code,
                        node_errors=node_errors,
                        details=parsed_body,
                        retriable=False,
                    )
                raise ComfyUIError(
                    redacted,
                    status_code=response.status_code,
                    node_errors=node_errors,
                    details=parsed_body,
                    retriable=response.status_code in self.retry_status_codes,
                )

            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        "Timeout en ComfyUI %s %s (intento %d/%d); reintentando en %.2fs",
                        method,
                        path,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    await self._sleep(delay)
                    continue
                raise ComfyUITimeoutError(
                    self._redact(
                        f"Timeout contactando ComfyUI en {method} {path} "
                        f"tras {attempt + 1} intentos: {exc}"
                    ),
                    retriable=True,
                ) from exc

            except httpx.TransportError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    delay = self._compute_backoff(attempt)
                    logger.warning(
                        "Error de transporte en ComfyUI %s %s (intento %d/%d); "
                        "reintentando en %.2fs",
                        method,
                        path,
                        attempt + 1,
                        self.max_retries + 1,
                        delay,
                    )
                    await self._sleep(delay)
                    continue
                raise ComfyUIConnectionError(
                    self._redact(
                        f"Error de conexión hacia ComfyUI en {method} {path} "
                        f"tras {attempt + 1} intentos: {exc}"
                    ),
                    retriable=True,
                ) from exc

        raise ComfyUIConnectionError(
            self._redact(f"Fallo agotando reintentos hacia ComfyUI: {last_exc}"),
            retriable=True,
        )

    async def system_stats(self) -> dict[str, Any]:
        """Consulta GET /system_stats."""
        resp = await self._request("GET", "/system_stats")
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def health_check(self) -> bool:
        """Devuelve True si ComfyUI responde 200 en /system_stats."""
        try:
            await self.system_stats()
            return True
        except ComfyUIError:
            return False

    async def get_object_info(self, node_class: str | None = None) -> dict[str, Any]:
        """Consulta GET /object_info o GET /object_info/{node_class}."""
        path = f"/object_info/{node_class}" if node_class else "/object_info"
        resp = await self._request("GET", path)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def submit_prompt(
        self,
        prompt: dict[str, Any],
        *,
        client_id: str | None = None,
        extra_data: dict[str, Any] | None = None,
        job_id: str | None = None,
        project_id: str | None = None,
    ) -> PromptSubmission:
        """Envía un workflow en formato API a POST /prompt."""
        if not isinstance(prompt, dict) or not prompt:
            err = ComfyUIExecutionError(
                "El grafo del prompt debe ser un objeto JSON no vacío en formato API de ComfyUI"
            )
            self._fail_job(err, job_id=job_id, project_id=project_id)
            raise err

        payload: dict[str, Any] = {
            "prompt": prompt,
            "client_id": client_id or self.client_id,
        }
        if extra_data:
            payload["extra_data"] = extra_data

        try:
            resp = await self._request("POST", "/prompt", json_body=payload)
        except ComfyUIError as exc:
            self._fail_job(exc, job_id=job_id, project_id=project_id)
            raise

        data = resp.json()
        if not isinstance(data, dict):
            err = ComfyUIExecutionError("Respuesta inválida de POST /prompt")
            self._fail_job(err, job_id=job_id, project_id=project_id)
            raise err

        node_errors = data.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            msg, _ = _extract_error_message_from_body(
                data, "ComfyUI reportó node_errors al encolar el prompt"
            )
            err = ComfyUIExecutionError(
                self._redact(msg),
                prompt_id=data.get("prompt_id"),
                node_errors=node_errors,
                details=data,
            )
            self._fail_job(
                err,
                job_id=job_id,
                project_id=project_id,
                prompt_id=data.get("prompt_id"),
            )
            raise err

        prompt_id = str(data.get("prompt_id") or "").strip()
        if not prompt_id:
            err = ComfyUIExecutionError(
                "ComfyUI no devolvió prompt_id en POST /prompt",
                details=data,
            )
            self._fail_job(err, job_id=job_id, project_id=project_id)
            raise err

        submission = PromptSubmission(
            prompt_id=prompt_id,
            number=int(data.get("number") or 0),
            node_errors={},
        )

        if job_id and self._store is not None:
            self._store.update_job(
                job_id,
                status="running",
                prompt_id=prompt_id,
            )
        if self._broker is not None:
            self._broker.publish(
                "job",
                {
                    "job_id": job_id,
                    "project_id": project_id,
                    "prompt_id": prompt_id,
                    "status": "running",
                    "number": submission.number,
                },
            )

        return submission

    async def get_queue(self) -> QueueStatus:
        """Consulta GET /queue."""
        resp = await self._request("GET", "/queue")
        data = resp.json()
        if not isinstance(data, dict):
            return QueueStatus()
        return QueueStatus(
            queue_running=data.get("queue_running") or [],
            queue_pending=data.get("queue_pending") or [],
        )

    async def get_history(self, prompt_id: str | None = None) -> dict[str, Any]:
        """Consulta GET /history/{prompt_id} o GET /history."""
        path = f"/history/{prompt_id}" if prompt_id else "/history"
        resp = await self._request("GET", path)
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def _inspect_history_entry(
        self,
        prompt_id: str,
        entry: dict[str, Any],
    ) -> PromptResult | None:
        status_block = entry.get("status")
        status_str = "success"
        completed = True
        messages: list[Any] = []

        if isinstance(status_block, dict):
            status_str = str(status_block.get("status_str") or "success")
            if "completed" in status_block:
                completed = bool(status_block.get("completed"))
            if isinstance(status_block.get("messages"), list):
                messages = status_block["messages"]

        if status_str == "error":
            err_detail = f"Error de ejecución en ComfyUI para prompt {prompt_id}"
            for msg in messages:
                if (
                    isinstance(msg, list)
                    and len(msg) >= 2
                    and msg[0] in {"execution_error", "execution_interrupted"}
                    and isinstance(msg[1], dict)
                ):
                    exc_text = (
                        msg[1].get("exception_message")
                        or msg[1].get("message")
                        or err_detail
                    )
                    node_type = msg[1].get("node_type") or msg[1].get("node_id") or ""
                    err_detail = (
                        f"{node_type}: {exc_text}" if node_type else str(exc_text)
                    )
                    break
            raise ComfyUIExecutionError(
                self._redact(err_detail),
                prompt_id=prompt_id,
                details=entry,
            )

        outputs = extract_outputs(entry)
        if not completed and not outputs:
            return None

        return PromptResult(
            prompt_id=prompt_id,
            status_str=status_str,
            completed=True,
            outputs=outputs,
            raw_history=entry,
        )

    async def poll_until_complete(
        self,
        prompt_id: str,
        *,
        poll_interval: float | None = None,
        timeout: float | None = None,
        job_id: str | None = None,
        project_id: str | None = None,
    ) -> PromptResult:
        """Sondea /queue y /history/{prompt_id} hasta que el prompt complete o falle."""
        interval = poll_interval if poll_interval is not None else self.poll_interval
        max_wait = timeout if timeout is not None else self.poll_timeout
        deadline = time.monotonic() + max_wait
        tick = 0

        try:
            while True:
                tick += 1
                history_map = await self.get_history(prompt_id)
                entry = history_map.get(prompt_id)
                if isinstance(entry, dict) and entry:
                    result = self._inspect_history_entry(prompt_id, entry)
                    if result is not None:
                        progress_max = max(tick, 1)
                        if job_id and self._store is not None:
                            self._store.update_job(
                                job_id,
                                status="done",
                                prompt_id=prompt_id,
                                progress_value=float(progress_max),
                                progress_max=float(progress_max),
                                current_node=None,
                                result={
                                    "prompt_id": prompt_id,
                                    "outputs": [o.model_dump() for o in result.outputs],
                                },
                            )
                        if self._broker is not None:
                            self._broker.publish(
                                "progress",
                                {
                                    "job_id": job_id,
                                    "project_id": project_id,
                                    "prompt_id": prompt_id,
                                    "status": "done",
                                    "value": float(progress_max),
                                    "max": float(progress_max),
                                    "node": None,
                                },
                            )
                            self._broker.publish(
                                "job",
                                {
                                    "job_id": job_id,
                                    "project_id": project_id,
                                    "prompt_id": prompt_id,
                                    "status": "done",
                                    "outputs": [o.model_dump() for o in result.outputs],
                                },
                            )
                        return result

                # Aún no está en /history: consultar /queue y emitir progreso de sondeo
                queue = await self.get_queue()
                is_running = prompt_id in queue.running_prompt_ids()
                current_status = "running" if is_running else "queued"
                estimated_max = float(max(tick + 1, 2))
                current_val = float(tick)

                if job_id and self._store is not None:
                    self._store.update_job(
                        job_id,
                        status="running",
                        prompt_id=prompt_id,
                        progress_value=current_val,
                        progress_max=estimated_max,
                    )
                if self._broker is not None:
                    self._broker.publish(
                        "progress",
                        {
                            "job_id": job_id,
                            "project_id": project_id,
                            "prompt_id": prompt_id,
                            "status": current_status,
                            "value": current_val,
                            "max": estimated_max,
                            "node": "poll",
                        },
                    )

                if time.monotonic() >= deadline:
                    raise ComfyUITimeoutError(
                        self._redact(
                            f"Timeout esperando completado del prompt "
                            f"{prompt_id} tras {max_wait:.2f}s"
                        ),
                        prompt_id=prompt_id,
                    )

                await self._sleep(interval)

        except ComfyUIError as exc:
            self._fail_job(
                exc,
                job_id=job_id,
                project_id=project_id,
                prompt_id=prompt_id,
            )
            raise

    def handle_ws_event(
        self,
        raw_message: str | bytes | dict[str, Any] | ComfyUIWsEvent,
        *,
        job_id: str | None = None,
        project_id: str | None = None,
    ) -> ComfyUIWsEvent | None:
        """Parsea un evento /ws de ComfyUI y lo proyecta al JobStore y al canal SSE."""
        event = (
            raw_message
            if isinstance(raw_message, ComfyUIWsEvent)
            else parse_ws_event(raw_message)
        )
        if event is None:
            return None

        if event.event_type == "progress":
            val = event.value if event.value is not None else 0.0
            max_val = event.max if event.max is not None else 1.0
            if job_id and self._store is not None:
                self._store.update_job(
                    job_id,
                    status="running",
                    prompt_id=event.prompt_id,
                    progress_value=val,
                    progress_max=max_val,
                    current_node=event.node,
                )
            if self._broker is not None:
                self._broker.publish(
                    "progress",
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "prompt_id": event.prompt_id,
                        "status": "running",
                        "value": val,
                        "max": max_val,
                        "node": event.node,
                    },
                )

        elif event.event_type == "executing":
            if event.node is not None:
                if job_id and self._store is not None:
                    self._store.update_job(
                        job_id,
                        status="running",
                        prompt_id=event.prompt_id,
                        current_node=event.node,
                    )
                if self._broker is not None:
                    self._broker.publish(
                        "progress",
                        {
                            "job_id": job_id,
                            "project_id": project_id,
                            "prompt_id": event.prompt_id,
                            "status": "running",
                            "node": event.node,
                        },
                    )
            else:
                # node=None en ComfyUI WS indica fin de ejecución del prompt
                if job_id and self._store is not None:
                    existing = self._store.get_job(job_id)
                    pmax = float((existing or {}).get("progress_max") or 1.0)
                    self._store.update_job(
                        job_id,
                        status="done",
                        prompt_id=event.prompt_id,
                        progress_value=pmax,
                        progress_max=pmax,
                        current_node=None,
                    )
                if self._broker is not None:
                    self._broker.publish(
                        "job",
                        {
                            "job_id": job_id,
                            "project_id": project_id,
                            "prompt_id": event.prompt_id,
                            "status": "done",
                        },
                    )

        elif event.event_type == "executed":
            if self._broker is not None:
                self._broker.publish(
                    "progress",
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "prompt_id": event.prompt_id,
                        "status": "running",
                        "node": event.node,
                        "executed_output": event.output,
                    },
                )

        elif event.event_type == "status":
            if self._broker is not None:
                self._broker.publish(
                    "job",
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "prompt_id": event.prompt_id,
                        "queue_remaining": event.queue_remaining,
                    },
                )

        elif event.event_type in {"execution_error", "execution_interrupted"}:
            err = ComfyUIExecutionError(
                self._redact(event.error_message or f"ComfyUI {event.event_type}"),
                prompt_id=event.prompt_id,
                details=event.raw_data,
            )
            self._fail_job(
                err,
                job_id=job_id,
                project_id=project_id,
                prompt_id=event.prompt_id,
            )
            raise err

        return event

    async def view_output(
        self,
        filename: str,
        *,
        subfolder: str = "",
        storage_type: str = "output",
    ) -> bytes:
        """Descarga bytes de una salida o input mediante GET /view."""
        resp = await self._request(
            "GET",
            "/view",
            params={
                "filename": filename,
                "subfolder": subfolder,
                "type": storage_type,
            },
        )
        return resp.content

    async def upload_image(
        self,
        data: bytes | Path,
        filename: str | None = None,
        *,
        subfolder: str = "",
        overwrite: bool = True,
        storage_type: str = "input",
        mime_type: str = "image/png",
    ) -> ComfyUIUploadResult:
        """Sube una imagen a ComfyUI mediante POST /upload/image."""
        if isinstance(data, Path):
            raw_bytes = data.read_bytes()
            effective_name = filename or data.name
        else:
            raw_bytes = data
            effective_name = filename or "upload.png"

        resp = await self._request(
            "POST",
            "/upload/image",
            files={"image": (effective_name, raw_bytes, mime_type)},
            data={
                "subfolder": subfolder,
                "overwrite": "true" if overwrite else "false",
                "type": storage_type,
            },
        )
        body = resp.json()
        return ComfyUIUploadResult(
            name=str(body.get("name") or effective_name),
            subfolder=str(body.get("subfolder") or subfolder),
            storage_type=str(body.get("type") or storage_type),
            overwrite=bool(body.get("overwrite", overwrite)),
        )

    async def upload_video(
        self,
        data: bytes | Path,
        filename: str | None = None,
        *,
        subfolder: str = "",
        overwrite: bool = True,
        storage_type: str = "input",
        mime_type: str = "video/mp4",
    ) -> ComfyUIUploadResult:
        """Sube un vídeo para VideoHelperSuite (VHS_LoadVideo).

        Verificado en Kosinkadink/ComfyUI-VideoHelperSuite (web/js/VHS.core.js):
        VHS sube vídeos mediante POST /upload/image con el campo multipart 'image'.
        """
        if isinstance(data, Path):
            raw_bytes = data.read_bytes()
            effective_name = filename or data.name
        else:
            raw_bytes = data
            effective_name = filename or "upload.mp4"

        return await self.upload_image(
            raw_bytes,
            filename=effective_name,
            subfolder=subfolder,
            overwrite=overwrite,
            storage_type=storage_type,
            mime_type=mime_type,
        )

    async def interrupt(self) -> dict[str, Any]:
        """Envía POST /interrupt para detener la ejecución activa."""
        resp = await self._request("POST", "/interrupt")
        data = resp.json()
        return data if isinstance(data, dict) else {"interrupted": True}

    async def free_memory(
        self,
        *,
        unload_models: bool = True,
        free_memory: bool = True,
    ) -> dict[str, Any]:
        """Envía POST /free para descargar modelos o liberar VRAM."""
        resp = await self._request(
            "POST",
            "/free",
            json_body={
                "unload_models": unload_models,
                "free_memory": free_memory,
            },
        )
        data = resp.json()
        return data if isinstance(data, dict) else {"freed": True}

    async def execute_prompt_job(
        self,
        prompt: dict[str, Any],
        *,
        job_id: str | None = None,
        project_id: str | None = None,
        template_id: str | None = None,
        poll_interval: float | None = None,
        timeout: float | None = None,
        persist_outputs: bool = True,
    ) -> PromptResult:
        """Ciclo de alto nivel: submit -> poll -> descarga /view -> registro en proyecto."""
        submission = await self.submit_prompt(
            prompt,
            job_id=job_id,
            project_id=project_id,
        )
        result = await self.poll_until_complete(
            submission.prompt_id,
            poll_interval=poll_interval,
            timeout=timeout,
            job_id=job_id,
            project_id=project_id,
        )

        if persist_outputs and project_id and self._store is not None:
            for out_file in result.outputs:
                content = await self.view_output(
                    out_file.filename,
                    subfolder=out_file.subfolder,
                    storage_type=out_file.storage_type,
                )
                mime = (
                    "video/mp4"
                    if out_file.filename.endswith(".mp4")
                    else "image/png"
                )
                meta: dict[str, Any] = {
                    "prompt_id": submission.prompt_id,
                    "node_id": out_file.node_id,
                    "comfy_filename": out_file.filename,
                    "subfolder": out_file.subfolder,
                    "storage_type": out_file.storage_type,
                }
                if template_id:
                    meta["template_id"] = template_id
                self._store.save_upload(
                    project_id,
                    filename=out_file.filename,
                    content_type=mime,
                    data=content,
                    max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
                    category="outputs",
                    job_id=job_id,
                    metadata=meta,
                )

        return result
