"""Notificaciones webhook salientes de Comfy Studio (M3-B, tarea 15 del plan).

Diseño:
- `WebhookNotifier`: dispatcher asíncrono no bloqueante con backlog acotado,
  reintentos con backoff, firma HMAC-SHA256 opcional (`X-Studio-Signature`) y
  redacción estricta de secretos antes de serializar el payload.
- `BrokerWebhookDispatcher`: suscriptor del `ProgressBroker` que traduce eventos
   internos (`job` done/failed/error/cancelled/interrupted, `done` de turno de
   agente) a
  eventos webhook (`job_finished`, `job_failed`, `job_interrupted`,
  `agent_turn_done`).
- La URL la aporta el operador vía `STUDIO_WEBHOOK_URL` (no es input de usuario);
  el secreto vía `STUDIO_WEBHOOK_SECRET` y se redacta en todas las salidas.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import SecretRedactor, StudioConfig
from .progress import ProgressBroker

logger = logging.getLogger("comfy_studio.webhooks")

WEBHOOK_BACKLOG = 128
WEBHOOK_TIMEOUT_S = 10.0
WEBHOOK_RETRIES = 3
WEBHOOK_RETRY_BASE_DELAY_S = 1.0

_TERMINAL_JOB_STATUS_TO_EVENT = {
    "done": "job_finished",
    "failed": "job_failed",
    "error": "job_failed",
    "cancelled": "job_failed",
    "interrupted": "job_interrupted",
}


class WebhookUrlError(ValueError):
    """URL de webhook rechazada por la guarda anti-SSRF (M3-SEC-001)."""


def _ip_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def validate_webhook_url(url: str, *, resolve: bool = True) -> str:
    """Valida una URL de webhook saliente (regla 5 de AGENTS.md, M3-SEC-001).

    Rechaza esquemas no HTTP(S), hosts vacíos, IPs literales de loopback/privadas/
    link-local (incluida la metadata `169.254.169.254`) y, con `resolve=True`,
    hostnames cuya resolución DNS apunte a esas redes. Devuelve la URL normalizada.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise WebhookUrlError("STUDIO_WEBHOOK_URL vacía")
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise WebhookUrlError(f"Esquema no permitido para webhook: {parsed.scheme!r}")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise WebhookUrlError("Webhook sin hostname")
    if host in ("localhost", "localhost.localdomain"):
        raise WebhookUrlError("Webhook a loopback (localhost) no permitido")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_forbidden(literal):
            raise WebhookUrlError(f"Webhook a red interna no permitido: {host}")
        return candidate
    if resolve:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            # Sin DNS (offline/hostname de test con transporte ASGI): el envío
            # fallará más adelante de forma natural; no bloqueamos el arranque.
            return candidate
        for info in infos:
            addr = info[4][0]
            try:
                ip = ipaddress.ip_address(addr.split("%", 1)[0])
            except ValueError:
                continue
            if _ip_is_forbidden(ip):
                raise WebhookUrlError(
                    f"Webhook {host!r} resuelve a red interna ({addr}); no permitido"
                )
    return candidate


def map_broker_event(
    event_type: str,
    data: dict[str, Any],
) -> tuple[str, dict[str, Any]] | None:
    """Traduce un evento del broker a un evento webhook; `None` si no aplica."""
    if event_type == "job":
        status = str(data.get("status") or "")
        mapped = _TERMINAL_JOB_STATUS_TO_EVENT.get(status)
        if mapped is None:
            return None
        payload: dict[str, Any] = {
            key: data[key]
            for key in ("job_id", "project_id", "prompt_id", "status")
            if data.get(key) is not None
        }
        if data.get("error"):
            payload["error"] = data["error"]
        return mapped, payload
    if event_type == "done":
        return (
            "agent_turn_done",
            {
                key: data[key]
                for key in ("project_id", "iterations", "stop_reason", "provider", "model")
                if data.get(key) is not None
            },
        )
    return None


class WebhookNotifier:
    """Envía eventos JSON a `STUDIO_WEBHOOK_URL` sin bloquear el event loop."""

    def __init__(
        self,
        *,
        url: str,
        secret: str | None = None,
        redactor: SecretRedactor,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = WEBHOOK_TIMEOUT_S,
        retries: int = WEBHOOK_RETRIES,
        retry_base_delay_s: float = WEBHOOK_RETRY_BASE_DELAY_S,
        backlog: int = WEBHOOK_BACKLOG,
        validate_url: bool = True,
    ) -> None:
        if validate_url:
            url = validate_webhook_url(url)
        self._url = url.strip()
        self._secret = (secret or "").strip() or None
        self._redactor = redactor
        self._timeout_s = timeout_s
        self._retries = max(1, int(retries))
        self._retry_base_delay_s = retry_base_delay_s
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=backlog)
        self._task: asyncio.Task[None] | None = None
        self._client = httpx.AsyncClient(transport=transport, timeout=timeout_s)
        self.sent_count = 0
        self.failed_count = 0
        self.dropped_count = 0

    # -- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="webhook-notifier")

    async def stop(self) -> None:
        if self._task is not None:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._client.aclose()

    # -- API pública ---------------------------------------------------------

    def fire(self, event: str, payload: dict[str, Any]) -> bool:
        """Encola un evento (no bloqueante). Devuelve `False` si el backlog está lleno."""
        body = {"event": event, **self._redactor.redact_value(dict(payload))}
        try:
            self._queue.put_nowait(body)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.warning("Webhook backlog lleno; evento %s descartado.", event)
            return False
        self.start()
        return True

    # -- entrega -------------------------------------------------------------

    def _sign(self, raw: bytes) -> str | None:
        if self._secret is None:
            return None
        return hmac.new(self._secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

    async def _deliver(self, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "comfy-studio-webhooks/1.0",
        }
        signature = self._sign(raw)
        if signature is not None:
            headers["X-Studio-Signature"] = signature

        last_error = "desconocido"
        for attempt in range(1, self._retries + 1):
            try:
                resp = await self._client.post(self._url, content=raw, headers=headers)
                if resp.status_code < 400:
                    self.sent_count += 1
                    return
                last_error = f"HTTP {resp.status_code}"
                # M3-SCR-015: 4xx (salvo 408/429) no se arregla reintentando
                if 400 <= resp.status_code < 500 and resp.status_code not in (408, 429):
                    break
            except httpx.HTTPError as exc:
                last_error = str(exc)
            if attempt < self._retries:
                await asyncio.sleep(self._retry_base_delay_s * attempt)

        self.failed_count += 1
        logger.warning(
            "Webhook %s falló tras %d intentos: %s",
            body.get("event"),
            self._retries,
            self._redactor.redact(last_error),
        )

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            await self._deliver(item)


class BrokerWebhookDispatcher:
    """Suscriptor del broker que reenvía eventos terminales al `WebhookNotifier`."""

    def __init__(self, broker: ProgressBroker, notifier: WebhookNotifier) -> None:
        self._broker = broker
        self._notifier = notifier
        self._queue = broker.subscribe()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="webhook-dispatcher")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._broker.unsubscribe(self._queue)

    async def _run(self) -> None:
        while True:
            event_type, data = await self._queue.get()
            mapped = map_broker_event(event_type, data)
            if mapped is None:
                continue
            self._notifier.fire(mapped[0], mapped[1])


def create_webhook_notifier(
    config: StudioConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> WebhookNotifier | None:
    """Crea el notifier si `STUDIO_WEBHOOK_URL` está definida y es válida; `None` si no.

    Una URL rechazada por la guarda anti-SSRF (M3-SEC-001) desactiva los webhooks
    con un ERROR en el log en vez de tumbar el arranque del servidor.
    """
    url = (config.studio_webhook_url or "").strip()
    if not url:
        return None
    try:
        safe_url = validate_webhook_url(url)
    except WebhookUrlError as exc:
        logger.error("STUDIO_WEBHOOK_URL rechazada (%s); webhooks desactivados.", exc)
        return None
    return WebhookNotifier(
        url=safe_url,
        secret=config.studio_webhook_secret,
        redactor=config.redactor(),
        transport=transport,
    )
