Warning: truncated output (original token count: 20239)
Total output lines: 1956

"""Cliente HTTP y parser de eventos WebSocket para ComfyUI (127.0.0.1:18188 / stub 39172)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
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
from comfy_studio.engines.preparedness import note_engine_run
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import (
    MEASUREMENT_STATUS_LOST,
    MEASUREMENT_STATUS_OK,
    ProjectStore,
)

logger = logging.getLogger("comfy_studio.comfyui_client")

_DEFAULT_RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})

#: Motivo que se guarda en la fila cuando el muestreador no dejó medición. Vive
#: aquí para que el log y la fila digan lo mismo.
_DRAIN_LOST_REASON = (
    "muestreador de /system_stats perdido "
    "(drenaje, excepción o muestra de cierre fallida)"
)

# --- Medición de VRAM/RAM durante un render (política, punto 5) -------------
#: Intervalo entre muestras periódicas de `/system_stats`.
_SAMPLE_INTERVAL_S = 2.0
#: Espera máxima al drenar el muestreador cuando el render ya terminó.
_SAMPLER_DRAIN_TIMEOUT_S = 2.0

# --- Descarga de modelos cuando no queda trabajo (política, puntos 3 y 5) ---
#: Espera máxima del cerrojo GPU para poder hacer `POST /free`. Si la GPU
#: sigue ocupada pasado este plazo se renuncia a la descarga: el render que la
#: está usando la hará al terminar, y así la cola no espera POR ENTRAR.
_GPU_UNLOAD_WAIT_S = 2.0
#: Presupuesto TOTAL de `POST /free` (descarga = mejor esfuerzo). Acota el
#: cerrojo: el camino largo eran `max_retries=3` con backoff de hasta 5 s y
#: 30 s por intento, es decir ~2 minutos de GPU secuestrada.
#: OJO: este ES el tiempo que la cola espera detrás de un `/free` colgado, porque
#: el POST se hace con el cerrojo cogido (F3). R2 acotó el techo, no la espera.
_FREE_MEMORY_BUDGET_S = 5.0

# Extensión -> MIME para salidas de ComfyUI (coherente con ALLOWED_MEDIA_MIMES
# de projects.py); extensiones fuera de este mapa no se persisten.
_OUTPUT_MIME_BY_EXT: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".qt": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".mp3": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".wav": "audio/wav",
    ".wave": "audio/wav",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/x-m4a",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".obj": "model/obj",
}


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


class ComfyUICancelledError(ComfyUIExecutionError):
    """El prompt fue cancelado explícitamente por su propietario."""


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
    """Extrae artefactos (images, gifs, video, videos, audio, 3d) de una entrada de /history."""
    outputs_by_node = history_entry.get("outputs")
    if not isinstance(outputs_by_node, dict):
        return []

    files: list[ComfyUIOutputFile] = []
    for node_id, node_out in outputs_by_node.items():
        if not isinstance(node_out, dict):
            continue
        for kind in ("gifs", "video", "videos", "images", "audio", "3d", "mesh", "meshes", "files"):
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



#: Claves donde las versiones de ComfyUI publican el driver del host.
_DRIVER_KEYS = ("driver_version", "driver", "nvidia_driver_version")


def _empty_peak() -> dict[str, Any]:
    """Pico medido aún vacío."""
    return {
        "vram_peak_mb": None,
        "ram_peak_mb": None,
        "gpu_name": None,
        "driver": None,
        "cuda": None,
        "samples": 0,
        #: True SOLO si la muestra de cierre (la que se toma al parar, con los
        #: modelos ya cargados) salió bien. Es la garantía que necesita
        #: `_drain_sampler` para decir «medido»: `samples >= 1` no la da, porque
        #: la primera muestra sale junto al `submit_prompt`, ANTES de que ComfyUI
        #: cargue los modelos, y su valor es el ocioso.
        "closing_ok": False,
    }


def _merge_stats_into_peak(peak: dict[str, Any], stats: dict[str, Any]) -> None:
    """Acumula en `peak` lo que diga UNA respuesta de `/system_stats`.

    Se mantiene FUERA del `try` que envuelve la llamada HTTP a propósito: si
    el payload llega con una forma inesperada (`devices` como dict, o
    `vram_used` no numérico) la excepción tiene que poder salir. Quien llama
    decide si ese error puede tumbar un render (ver `_drain_sampler`).
    """
    devices = stats.get("devices") or []
    if devices and isinstance(devices[0], dict):
        device = devices[0]
        vram_total = device.get("vram_total") or 0
        vram_used = device.get("vram_used") or 0
        if vram_used:
            used_mb = int(vram_used / (1024 * 1024))
            if peak["vram_peak_mb"] is None or used_mb > peak["vram_peak_mb"]:
                peak["vram_peak_mb"] = used_mb
                peak["vram_total_mb"] = int(vram_total / (1024 * 1024)) if vram_total else None
        if device.get("name"):
            peak["gpu_name"] = device["name"]
        if peak["driver"] is None:
            for key in _DRIVER_KEYS:
                if device.get(key):
                    peak["driver"] = str(device[key])
                    break
    system = stats.get("system") or {}
    ram_used = system.get("ram_used")
    if ram_used:
        used_mb = int(ram_used / (1024 * 1024))
        if peak["ram_peak_mb"] is None or used_mb > peak["ram_peak_mb"]:
            peak["ram_peak_mb"] = used_mb
    if stats.get("cuda_version"):
        peak["cuda"] = stats["cuda_version"]
    peak["samples"] += 1


def _discard_sampler_error(task: asyncio.Task[dict[str, Any]]) -> None:
    """Marca como leída la excepción del muestreador: nunca debe avisar a nadie.

    El drenaje normal ya la consume, pero en el camino de fallo/cancelación la
    tarea se cancela sin esperarla y asyncio se quejaría por una excepción no
    recuperada.
    """
    if not task.cancelled():
        task.exception()


async def sample_system_stats_peak(
    client: ComfyUIClient,
    *,
    stop: asyncio.Event,
    interval: float = _SAMPLE_INTERVAL_S,
) -> dict[str, Any]:
    """Muestrea `/system_stats` mientras dura un render y devuelve el pico medido.

    Es la única forma honesta de saber cuánta VRAM/RAM pidió de verdad un
    trabajo: las declaraciones de las plantillas son estimativas. Falla en
    silencio si ComfyUI no responde: una medición ausente no debe tumbar el
    render.

    Al parar toma UNA muestra más. Hace falta: la primera llamada sale en
    cuanto el loop la agenda, es decir junto al `submit_prompt` y ANTES de que
    ComfyUI cargue los modelos, así que un render más corto que `interval` se
    quedaba con el valor ocioso y eso era lo que se persistía en la receta y en
    el modelo de coste. El llamador espera esa muestra final sin soltar el
    cerrojo GPU, para que la VRAM del render siguiente no se cuele en ella.

    El pico devuelto lleva `closing_ok`, que dice si ESA muestra final salió
    bien. Es la única que ve el pico real, así que si falla el pico que queda es
    el ocioso: sin el flag, «al menos una muestra entró» (`samples >= 1`) se
    leía como «medido» y una medición perdida se escribía con `status="ok"` —
    justo el defecto que R6 y R50 arreglaban, vuelto por la puerta de atrás.
    """
    peak = _empty_peak()
    while not stop.is_set():
        try:
            stats = await client.system_stats()
        except Exception:  # noqa: BLE001 - la medición nunca rompe el render
            return peak
        _merge_stats_into_peak(peak, stats)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    try:
        stats = await client.system_stats()
    except Exception:  # noqa: BLE001 - la medición nunca rompe el render
        return peak
    _merge_stats_into_peak(peak, stats)
    peak["closing_ok"] = True
    return peak


class _GpuGate:
    """Exclusión mutua de UN solo trabajo GPU pesado, independiente del loop.

    La exclusión es del hardware, no de un event loop: un `asyncio.Semaphore`
    queda atado al loop donde se creó y, compartido entre dos loops, se cuelga
    (el waiter del segundo loop no se despierta nunca). Producción usa DOS loops:
    el principal de la app para los renders HTTP/agente y un `asyncio.run` dentro
    de `run_in_threadpool` para la orquestación. Por eso el cerrojo vive sobre
    `threading.Lock` (válido en cualquier loop y cualquier hilo) y la espera se
    hace con `asyncio.sleep`, que sí es loop-local.
    """

    def __init__(self, *, poll_interval: float = 0.05) -> None:
        self._lock = threading.Lock()
        self._poll_interval = poll_interval

    @property
    def locked(self) -> bool:
        return self._lock.locked()

    async def acquire(self, *, timeout: float | None = None) -> bool:
        """Adquiere el cerrojo esperando como máximo `timeout` segundos.

        Devuelve False si no se obtuvo dentro del plazo, para que el llamador
        pueda renunciar en lugar de esperar detrás de un render largo. Con
        `timeout=None` espera sin límite (caso del cómputo GPU, que es
        exactamente lo que hay que serializar).
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        # Adquisición no bloqueante + espera asíncrona: nunca se bloquea el loop
        # ni se depende del loop donde se adquirió el cerrojo.
        while not self._lock.acquire(blocking=False):
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self._poll_interval)
        return True

    def release(self) -> None:
        self._lock.release()

    async def __aenter__(self) -> _GpuGate:
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._lock.release()


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
        unload_when_idle: bool = True,
        gpu_gate: _GpuGate | None = None,
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
        # Política (puntos 3 y 5): liberar VRAM cuando no queda trabajo.
        self._unload_when_idle = bool(unload_when_idle)
        # Política (punto 3): «Un trabajo GPU pesado a la vez». La exclusión
        # mutua vive en el cliente porque es el único punto por el que pasa
        # toda submission a ComfyUI (registro de plantillas, recuperación al
        # arrancar y runner de motor), así ningún ejecutor puede colarse.
        # Se crea UNA sola vez y no se rehace por loop (ver `_GpuGate`): los
        # renders del loop principal y los de la orquestación se excluyen entre
        # sí. El parámetro sigue permitiendo inyectar uno compartido, pero solo
        # un `_GpuGate`: un `asyncio.Semaphore` ajeno queda atado al loop donde
        # se construyó y, compartido entre los dos loops de producción, se
        # cuelga. Antes se aceptaba cualquier objeto con `__aenter__`, así que
        # ese peligro estaba a una inyección de distancia; ahora se rechaza en
        # construcción en lugar de confiar en la anotación de tipos.
        if gpu_gate is not None and not isinstance(gpu_gate, _GpuGate):
            raise TypeError(
                "gpu_gate debe ser un _GpuGate (exclusión independiente del "
                f"event loop); se recibió {type(gpu_gate).__name__}"
            )
        self._gpu_gate = gpu_gate
        self._gpu_gate_guard = threading.Lock()

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
        eff_prompt_id = prompt_id or error.prompt_id
        if job_id and self._store is not None:
            existing = self._store.get_job(job_id)
            if existing is not None:
                # `cancelled` e `interrupted` son terminales "pegajosos": un poll tardío
                # no debe resucitarlos ni sobrescribirlos.
                if existing.get("status") in ("cancelled", "interrupted"):
                    return
                # Poll antiguo de un prompt ya re-encolado con otro prompt_id: no
                # machacar el estado del job vivo (reconciliación tras reinicio de ComfyUI).
                if (
                    eff_prompt_id
                    and existing.get("prompt_id")
                    and existing.get("prompt_id") != eff_prompt_id
                    and existing.get("status") in ("queued", "running")
                ):
                    return
        redacted_msg = self._redact(error.message)
        status = "cancelled" if isinstance(error, ComfyUICancelledError) else "error"
        error.message = redacted_msg
        error.args = (redacted_msg,)
        if error.prompt_id is None and eff_prompt_id is not None:
            error.prompt_id = eff_prompt_id
        if job_id and self._store is not None:
            self._store.update_job(
                job_id,
                status=status,
                prompt_id=eff_prompt_id,
                error=redacted_msg,
                result={
                    "error": redacted_msg,
                    "status": status,
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
                    "prompt_id": eff_prompt_id,
                    "status": status,
                    "error": redacted_msg,
                },
  …8239 tokens truncated…      except TimeoutError:
            # El orden NO es para hacer alcanzable el timeout: `TimeoutError`
            # (y `asyncio.TimeoutError`, que ES `TimeoutError`) ya lo cubre el
            # `except Exception` de abajo, porque su MRO pasa por `Exception`
            # (`TimeoutError -> OSError -> Exception`). Se nombra ANTES para
            # darle su propio mensaje —el drenaje se agotó, no reventó el
            # muestreador— y sin traceback: un timeout aquí es esperado, no un
            # defecto del código. Ver el docstring de arriba, que ya lo dice.
            logger.warning(
                "El muestreador de /system_stats no acabó en %.1f s: la medición "
                "de VRAM/RAM de este render se pierde",
                _SAMPLER_DRAIN_TIMEOUT_S,
            )
            peak = {}
        except Exception:  # noqa: BLE001 - la medición nunca rompe el render
            logger.warning(
                "El muestreador de /system_stats falló; la medición de VRAM/RAM "
                "de este render se pierde",
                exc_info=True,
            )
            peak = {}
        finally:
            if not sample_task.done():
                sample_task.cancel()
        if not isinstance(peak, dict):
            return {}, False
        # «Medido» es «la muestra de cierre salió bien», NO «entró al menos una
        # muestra». La primera muestra se toma junto al `submit_prompt`, antes de
        # que ComfyUI cargue los modelos: si la de cierre falla, el pico que
        # queda es el ocioso y escribir `status="ok"` con él fabrica un veredicto
        # de encaje falso (receta, `assess_vram_fit`, coste). Ver
        # `sample_system_stats_peak`.
        return peak, bool(peak.get("closing_ok"))

    def _acquire_gpu_gate(self) -> _GpuGate:
        """Cerrojo de un solo trabajo GPU pesado, creado UNA sola vez.

        No se rehace cuando cambia el loop: `_GpuGate` se apoya en
        `threading.Lock`, así que vale para el loop principal y para el
        `asyncio.run` de la orquestación. Rehacerlo por loop era el bug: el
        segundo loop descartaba el cerrojo compartido y los dos renders se
        solapaban en la GPU. El `threading.Lock` de construcción evita que dos
        loops que entren a la vez fabriquen cada uno el suyo.
        """
        if self._gpu_gate is None:
            with self._gpu_gate_guard:
                if self._gpu_gate is None:
                    self._gpu_gate = _GpuGate()
        return self._gpu_gate

    async def _release_gpu_when_idle(self, *, exclude_job_id: str | None = None) -> None:
        """Descarga los modelos cuando no queda trabajo (política, puntos 3 y 5).

        «Do not keep every engine or model resident in RAM or VRAM merely to
        describe the studio as ready» y «sequential scheduling and unloading
        inactive models». `POST /free` existía pero nunca se llamaba en
        producción: los modelos se quedaban residentes tras cada render.

        Solo se libera cuando **no queda ningún job en vuelo**, para no penalizar
        el rendimiento de una cola con trabajo. `exclude_job_id` es el render que
        acaba de terminar: se llama dentro del cerrojo, antes de marcar el job
        como terminal, así que sin excluirlo se creería que sigue habiendo
        trabajo y nunca se descargaría nada. Fallar aquí nunca debe tumbar un
        render que ya ha terminado y persistido.
        El POST va con presupuesto TOTAL (`_FREE_MEMORY_BUDGET_S`): descargar
        modelos es mejor esfuerzo, y sin ese techo el camino largo de
        `max_retries=3` + backoff podía secuestrar la GPU ~2 minutos. El cerrojo
        NO se toma aquí sino en `_unload_after_render`, para que el llamador
        decida cuánto GPU se invierte en la descarga.

        ATENCIÓN al límite real de la cola: el POST se hace CON el cerrojo
        cogido, así que un `/free` colgado retiene la GPU `_FREE_MEMORY_BUDGET_S`
        como mucho —no los ~2 minutos de antes, pero tampoco «nada». Lo que sí
        está acotado es la *espera* para entrar (`_GPU_UNLOAD_WAIT_S`).
        """
        if not self._unload_when_idle:
            return
        try:
            if self._has_pending_work(exclude_job_id=exclude_job_id):
                return
            await asyncio.wait_for(
                self.free_memory(unload_models=True, free_memory=True),
                timeout=_FREE_MEMORY_BUDGET_S,
            )
        except Exception:  # noqa: BLE001 - liberar VRAM nunca rompe un render
            logger.debug("No se pudo liberar VRAM tras el render", exc_info=True)

    async def _unload_after_render(self, *, exclude_job_id: str | None = None) -> None:
        """Descarga modelos tras un render, con exclusión GPU corta y acotada.

        Reconciliación de las tres restricciones que parecían incompatibles:

        - **F2** (la medición es del trabajo GPU, no de la cola ni del drenaje):
          el cerrojo se suelta justo después de la última muestra, así que ni la
          descarga ni el drenaje entran en `duration_s`.
        - **F3** (`POST /free` nunca coincide con el cómputo GPU de otro render):
          el POST se hace CON el cerrojo cogido, porque descargar modelos
          mientras otro render está cargando los suyos es exactamente el fallo
          que esa regla prohíbe.
        - **R2** (el cerrojo no puede bloquear la cola): no se espera el cerrojo
          sin límite ni se mantiene sobre el presupuesto de reintentos. Se espera
          como máximo `_GPU_UNLOAD_WAIT_S` y el POST tiene su propio presupuesto
          `_FREE_MEMORY_BUDGET_S`. Si pasado ese plazo la GPU sigue ocupada se
          RENUNCIA a la descarga: el render que la está usando la hará al
          terminar.

          Medido antes de esto: cancelar un render con el muestreador colgado
          retenía el cerrojo 5 s, y un `/free` colgado hasta ~2 minutos, con toda
          la cola de los dos event loops girando.

          Lo que R2 acotó fue el techo, no la existencia de la espera: el POST se
          hace CON el cerrojo cogido (F3), así que un `/free` colgado retiene la
          GPU `_FREE_MEMORY_BUDGET_S` y el render siguiente espera ese rato. No
          es «la cola nunca espera»: es «la cola espera como máximo el
          presupuesto».

        NO debe llamarse con el cerrojo ya cogido: es un `threading.Lock` sin
        reentrada. El cerrojo se vuelve a tomar AQUÍ (la misma instancia, pero
        fuera del span de `execute_prompt_job`), y ese es el único llamador.
        """
        gate = self._acquire_gpu_gate()
        if not await gate.acquire(timeout=_GPU_UNLOAD_WAIT_S):
            logger.debug("GPU ocupada por otro render: se pospone POST /free")
            return
        try:
            await self._release_gpu_when_idle(exclude_job_id=exclude_job_id)
        finally:
            gate.release()

    def _has_pending_work(self, *, exclude_job_id: str | None = None) -> bool:
        """True si queda algún job sin estado terminal (salvo `exclude_job_id`).

        La consulta es del store (`ProjectStore.has_pending_work`): UN `SELECT`
        con `LIMIT 1` sobre los estados no terminales, que no materializa
        ninguna fila. Las versiones anteriores pedían `list_jobs()` (completo o
        por estado) y `_row_to_job` hace `json.loads` de `params_json` y
        `result_json` por fila devuelta: eso se ejecutaba bajo el cerrojo GPU y
        serializaba la cola de renders detrás de un barrido de SQLite que crece
        con cada render. Con el `LIMIT` del store, el trabajo real de este guard
        es acotado y el filtro por estado vive en el mismo SQL que lo aplica.

        Falla CERRADO (True) si el store falla —incluido un store que no
        implemente el método—: dar por hecho que no hay nada en vuelo y descargar
        modelos es el lado inseguro (F3). El `timeout=15.0` + WAL del store hace
        que un escritor concurrente pueda lanzar `sqlite3.OperationalError`, así
        que el camino de error no es teórico.

        Sin store TAMBIÉN falla cerrado. Antes devolvía False («no queda nada») y
        un cliente sin store descargaba los modelos después de CADA render
        aunque la cola estuviera llena: es el mismo lado inseguro de la R4. Sin
        store no hay forma de saber si queda trabajo, y la duda se resuelve a
        favor de no tocar la GPU. En producción el cliente siempre se construye
        con store (`app.py`), así que esto solo alcanza a usos sin proyecto.
        """
        if self._store is None:
            return True
        try:
            return bool(self._store.has_pending_work(exclude_job_id))
        except Exception:  # noqa: BLE001 - ante la duda, queda trabajo
            logger.debug(
                "No se pudo comprobar si queda trabajo pendiente; se asume que sí",
                exc_info=True,
            )
            return True

    async def _record_render_measurement(
        self,
        *,
        duration_s: float,
        template_id: str | None,
        job_id: str | None,
        project_id: str | None,
        params: dict[str, Any] | None,
        peak: dict[str, Any],
        measured: bool = True,
    ) -> None:
        """Registra el render que acaba de terminar (política, punto 5).

        `peak` viene de la muestra tomada *durante* el trabajo (ver
        `sample_system_stats_peak`, cuya última muestra se espera antes de soltar
        el cerrojo GPU), no de una estimativa.
        `duration_s` se mide DENTRO del cerrojo GPU (ventana = trabajo GPU), no
        al registrar: si se calculara aquí incluiría el drenaje del muestreador y
        la descarga de modelos. Guardar la medición nunca debe tumbar el render.

        `measured=False` marca la fila con el estado de medición perdida: el
        muestreador se agotó, reventó o no consiguió la muestra de cierre —la
        única que ve el pico real, porque la primera sale antes de que ComfyUI
        cargue los modelos— y no hay pico que llevarse a la boca.
        Escribirla igual con `status="ok"` hacía que `recipe_for_template` —que
        filtra justo por ese estado— la contara como una medición buena, así que
        la receta y `assess_vram_fit` razonaban sin dato y sin avisar de nada.
        El esquema solo admite `('ok','error')`, así que «perdida» se registra
        con el estado no-ok y el motivo en `error` (ver el comentario de las
        constantes en `projects.py`).
        """
        if template_id and self._store is not None:
            self._store.record_render_measurement(
                template_id=template_id,
                params=params,
                job_id=job_id,
                project_id=project_id,
                duration_s=round(max(0.0, duration_s), 2),
                vram_peak_mb=peak.get("vram_peak_mb"),
                ram_peak_mb=peak.get("ram_peak_mb"),
                gpu_name=peak.get("gpu_name"),
                # El driver sale de la medición, no de `strict-check`: la receta
                # debe describir la máquina donde se midió de verdad.
                driver=peak.get("driver"),
                cuda=peak.get("cuda"),
                status=(
                    MEASUREMENT_STATUS_OK
                    if measured
                    else MEASUREMENT_STATUS_LOST
                ),
                error=None if measured else _DRAIN_LOST_REASON,
            )

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
        cancel_event: Any | None = None,
    ) -> PromptResult:
        """Ciclo de alto nivel: submit -> poll -> descarga /view -> registro en proyecto."""
        # Exclusión mutua del trabajo GPU pesado: un solo render a la vez. Se
        # suelta con `async with`, así que también se libera si el render falla
        # o se cancela.
        #
        # ALCANCE DEL CERROJO: submit + poll + la última muestra del muestreador.
        # Nada más. La descarga de salidas y el registro en proyecto son I/O y
        # quedan fuera, y la descarga de modelos se hace fuera con su propia
        # exclusión corta (`_unload_after_render`). Las tres restricciones se
        # reconcilian así:
        #
        # - **F2** (la medición es del trabajo GPU, no de la cola ni del drenaje):
        #   `started_at` se toma al entrar, la última muestra se espera antes de
        #   soltar, y `duration_s` se corta JUSTO AL TERMINAR EL POLL, antes de
        #   drenar: `duration_s` describe el trabajo GPU de este render y el
        #   drenaje se mide y se registra aparte.
        # - **F3** (`POST /free` nunca coincide con el cómputo GPU de otro render):
        #   la descarga se hace con el cerrojo cogido, porque descargar modelos
        #   mientras otro render está cargando los suyos es justo lo que esa
        #   regla prohíbe. Se toma FUERA del span principal, con espera acotada.
        # - **R2** (el cerrojo no puede bloquear la cola): el camino de
        #   fallo/cancelación no espera al muestreador — lo cancela y sale, así
        #   que el cerrojo se suelta de inmediato (antes: 5 s). Y `POST /free`
        #   espera el cerrojo como máximo `_GPU_UNLOAD_WAIT_S` y consume como
        #   mucho `_FREE_MEMORY_BUDGET_S` (antes: hasta ~2 minutos de GPU
        #   secuestrada por el presupuesto de reintentos).
        #
        # Lo único que se espera dentro del span en el camino correcto es la
        # última muestra del muestreador, acotada a `_SAMPLER_DRAIN_TIMEOUT_S`.
        # Sacarla del span sería peor: la muestra se tomaría después de soltar,
        # con el render siguiente ya cargando modelos, y el pico medido pasaría a
        # ser el de ese otro render (que es lo que F2 y R6 existen para evitar).
        # Ese drenaje SÍ retiene el cerrojo (R49): un `/system_stats` colgado
        # mantiene la GPU como máximo `_SAMPLER_DRAIN_TIMEOUT_S`, y el render
        # siguiente espera eso y no más.
        async with self._acquire_gpu_gate():
            # `started_at` se toma DENTRO del cerrojo: `duration_s` tiene que
            # medir el trabajo GPU, no el tiempo que el render pasó en cola.
            started_at = time.monotonic()
            # El pico de VRAM/RAM se muestrea MIENTRAS dura el render y YA con el
            # cerrojo cogido, justo antes de enviar el prompt: muestrearlo antes
            # mediría la VRAM del render anterior mientras este espera su turno.
            stop_sampling = asyncio.Event()
            sample_task = asyncio.create_task(
                sample_system_stats_peak(
                    self, stop=stop_sampling, interval=_SAMPLE_INTERVAL_S
                )
            )
            sample_task.add_done_callback(_discard_sampler_error)
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise ComfyUICancelledError(
                        "El render fue cancelado antes de enviarse",
                        details={"cancelled": True},
                    )
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
                    finalize_job=False,
                    cancel_event=cancel_event,
                )
            except BaseException as exc:
                # El muestreador se para con el cerrojo todavía cogido: si no,
                # la tarea quedaría girando en `while not stop.is_set()` para
                # siempre. NO se espera a que acabe: un fallo o una cancelación
                # tienen que salir ya, y `_discard_sampler_error` evita el aviso
                # de excepción no recuperada. Medido antes de esto: cancelar un
                # render con el muestreador colgado retenía el cerrojo 5 s.
                stop_sampling.set()
                sample_task.cancel()
                if isinstance(exc, ComfyUICancelledError):
                    # This cancellation was requested for our own prompt, so
                    # it is safe to unload while the GPU gate is still held.
                    # Mark before the idle check so this job does not block its
                    # own cleanup; other queued work still prevents /free.
                    self._fail_job(
                        exc,
                        job_id=job_id,
                        project_id=project_id,
                        prompt_id=exc.prompt_id,
                    )
                    await self._release_gpu_when_idle(exclude_job_id=job_id)
                raise
            # Render correcto: se pide el paro y se espera al muestreador SIN
            # soltar el cerrojo. Su última muestra (ver
            # `sample_system_stats_peak`) es la que cierra la ventana de medición
            # justo al final del trabajo GPU: es la diferencia entre medir el
            # pico de ESTE render y quedarse con el valor ocioso que publicaba
            # ComfyUI antes de cargar los modelos.
            #
            # `duration_s` se corta AQUÍ, con el poll ya terminado y ANTES de
            # drenar (R48). El drenaje no es trabajo GPU de este render: un
            # `/system_stats` lento (1.5 s medidos) se colaba entero en
            # `duration_s`, y de ahí en `duration_s_avg/_max/_last` de la receta
            # y en el coste. El drenaje se mide y se registra aparte, para que se
            # vea sin inventarse el tiempo del render.
            duration_s = time.monotonic() - started_at
            drain_started_at = time.monotonic()
            stop_sampling.set()
            peak, measured = await self._drain_sampler(sample_task)
            drain_s = time.monotonic() - drain_started_at
            if not measured:
                # R66: la medición se perdió y el ÚNICO rastro era un
                # `logger.debug`, que producción descarta (uvicorn corre con
                # `log_level='info'`). El camino no rompe: el job acaba `done`,
                # `note_engine_run('comfyui', True)` da el render por bueno y lo
                # único que queda es una fila `render_measurements` con
                # `status="error"` que nadie mira en caliente. Se promueve a
                # WARNING y se lleva `_DRAIN_LOST_REASON` para que el log y la
                # fila digan exactamente lo mismo (mismo defecto que R56, otra
                # vez por el canal del log).
                logger.warning(
                    "Medición de VRAM/RAM perdida para este render: %s "
                    "(drenaje %.3f s)",
                    _DRAIN_LOST_REASON,
                    drain_s,
                )
            elif drain_s > 0.05:
                # El drenaje lento pero con la medición intacta no molesta a
                # nadie: se anuncia solo en debug. En el camino normal son unos
                # pocos ms y no se dice nada.
                logger.debug(
                    "Drenaje del muestreador: %.3f s (medición conservada)",
                    drain_s,
                )

        # Cerrojo soltado: lo que queda ya no es cómputo GPU. La descarga de
        # modelos se hace fuera, con su propia exclusión corta y acotada (ver
        # `_unload_after_render`): un `POST /free` colgado no puede retener la
        # GPU los ~2 minutos de su presupuesto de reintentos.
        await self._unload_after_render(exclude_job_id=job_id)

        await self._record_render_measurement(
            duration_s=duration_s,
            template_id=template_id,
            job_id=job_id,
            project_id=project_id,
            params=prompt,
            peak=peak if isinstance(peak, dict) else {},
            measured=measured,
        )

        if job_id and self._store is not None:
            existing = self._store.get_job(job_id)
            if existing and existing.get("status") == "cancelled":
                return result

        saved_asset_ids: list[str] = []
        try:
            downloaded_outputs: list[tuple[ComfyUIOutputFile, bytes]] = []
            for out_file in result.outputs:
                content = await self.view_output(
                    out_file.filename,
                    subfolder=out_file.subfolder,
                    storage_type=out_file.storage_type,
                    prompt_id=submission.prompt_id,
                )
                downloaded_outputs.append((out_file, content))

            if persist_outputs and project_id and self._store is not None:
                for out_file, content in downloaded_outputs:
                    ext = Path(out_file.filename).suffix.lower()
                    mime = _OUTPUT_MIME_BY_EXT.get(ext)
                    if mime is None:
                        logger.warning(
                            "Salida de ComfyUI con extensión no soportada (%s) en "
                            "prompt %s; se omite su guardado.",
                            out_file.filename,
                            submission.prompt_id,
                        )
                        continue
                    meta: dict[str, Any] = {
                        "prompt_id": submission.prompt_id,
                        "node_id": out_file.node_id,
                        "comfy_filename": out_file.filename,
                        "subfolder": out_file.subfolder,
                        "storage_type": out_file.storage_type,
                    }
                    if template_id:
                        meta["template_id"] = template_id
                    saved = self._store.save_upload(
                        project_id,
                        filename=out_file.filename,
                        content_type=mime,
                        data=content,
                        max_upload_bytes=DEFAULT_MAX_UPLOAD_BYTES,
                        category="outputs",
                        job_id=job_id,
                        metadata=meta,
                    )
                    if isinstance(saved, dict) and saved.get("id"):
                        saved_asset_ids.append(str(saved["id"]))
        except ComfyUIError as exc:
            if project_id and self._store is not None:
                for aid in saved_asset_ids:
                    self._store.delete_asset(project_id, aid)
            self._fail_job(
                exc,
                job_id=job_id,
                project_id=project_id,
                prompt_id=submission.prompt_id,
            )
            raise
        except Exception as exc:
            if project_id and self._store is not None:
                for aid in saved_asset_ids:
                    self._store.delete_asset(project_id, aid)
            persist_err = ComfyUIError(
                self._redact(
                    f"Error persistiendo salidas de ComfyUI para prompt "
                    f"{submission.prompt_id}: {exc}"
                ),
                prompt_id=submission.prompt_id,
            )
            self._fail_job(
                persist_err,
                job_id=job_id,
                project_id=project_id,
                prompt_id=submission.prompt_id,
            )
            raise persist_err from exc

        self._complete_job(
            result,
            job_id=job_id,
            project_id=project_id,
        )
        note_engine_run("comfyui", True)
        return result
