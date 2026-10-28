"""Bus SSE (/studio/api/progress) para eventos de job, agente y heartbeat."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from comfy_studio.config import SecretRedactor


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def format_sse_event(event_type: str, data: dict[str, Any]) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event_type}\ndata: {payload}\n\n"


class ProgressBroker:
    """Broker pub/sub en memoria para el canal SSE /studio/api/progress."""

    def __init__(
        self,
        history_limit: int = 100,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._history: deque[tuple[str, dict[str, Any]]] = deque(maxlen=history_limit)
        self._subscribers: set[asyncio.Queue[tuple[str, dict[str, Any]]]] = set()
        self._seq = 0
        self._redactor = redactor

    def publish(self, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        self._seq += 1
        enriched = {
            "type": event_type,
            "seq": self._seq,
            "ts": _utc_now_iso(),
            **data,
        }
        if self._redactor is not None:
            enriched = self._redactor.redact_value(enriched)
        self._history.append((event_type, enriched))
        for q in list(self._subscribers):
            try:
                q.put_nowait((event_type, enriched))
            except asyncio.QueueFull:
                pass
        return enriched

    def recent_events(self) -> list[tuple[str, dict[str, Any]]]:
        return list(self._history)

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        """Registra una cola suscriptora y la devuelve (M3-B: dispatcher de webhooks)."""
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        self._subscribers.discard(queue)

    async def stream(
        self,
        *,
        heartbeat_interval: float = 15.0,
        replay: bool = False,
        max_events: int | None = None,
    ) -> AsyncIterator[str]:
        """Genera frames SSE sin buffering, comenzando con un heartbeat inmediato."""
        queue = self.subscribe(maxsize=256)
        emitted = 0
        interval = max(0.01, float(heartbeat_interval))
        try:
            # Emitir primer heartbeat inmediato (< 50 ms) para evitar buffering en proxies
            initial_hb = {
                "type": "heartbeat",
                "ts": _utc_now_iso(),
            }
            yield format_sse_event("heartbeat", initial_hb)
            emitted += 1
            if max_events is not None and emitted >= max_events:
                return

            if replay:
                for ev_type, ev_data in list(self._history):
                    yield format_sse_event(ev_type, ev_data)
                    emitted += 1
                    if max_events is not None and emitted >= max_events:
                        return

            while True:
                try:
                    ev_type, ev_data = await asyncio.wait_for(
                        queue.get(), timeout=interval
                    )
                    yield format_sse_event(ev_type, ev_data)
                except TimeoutError:
                    hb = {
                        "type": "heartbeat",
                        "ts": _utc_now_iso(),
                    }
                    yield format_sse_event("heartbeat", hb)
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return
        finally:
            self.unsubscribe(queue)
