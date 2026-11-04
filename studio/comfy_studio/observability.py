"""Observabilidad del studio-server: request IDs y logs JSON opcionales (M7-OPS-005).

- Cada petición HTTP recibe un `X-Request-Id` (se acepta una entrante segura,
  si no se genera `uuid4`), se expone en la respuesta y queda disponible en el
  `ContextVar` para que los logs lleven el mismo ID dentro del request.
- Con `STUDIO_JSON_LOGS=1`, `server.py` instala un formatter JSON en el root
  logger. La redacción de secretos no se relaja: `install_log_redaction` añade
  `RedactingLogFilter` a los handlers y los filtros de handler ejecutan antes
  del format, así que el JSON emitido ya va redactado.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_request_id_ctx: ContextVar[str] = ContextVar("studio_request_id", default="-")


def current_request_id() -> str:
    """Request ID activo en el contexto async actual (`-` si no hay)."""
    return _request_id_ctx.get()


def new_request_id(request: Request) -> str:
    """Devuelve la cabecera entrante si es segura; si no, genera un uuid4 hex."""
    candidate = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
    if _REQUEST_ID_RE.match(candidate):
        return candidate
    return uuid.uuid4().hex


def bind_request_id(request_id: str) -> Token[str]:
    return _request_id_ctx.set(request_id)


def unbind_request_id(token: Token[str]) -> None:
    _request_id_ctx.reset(token)


class JsonLogFormatter(logging.Formatter):
    """Una línea JSON por registro, con el request_id del contexto."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "request_id": current_request_id(),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def json_logs_enabled(env: Mapping[str, str] | None = None) -> bool:
    source = env if env is not None else os.environ
    return source.get("STUDIO_JSON_LOGS", "").strip().lower() in {"1", "true", "yes", "on"}


def install_json_logging() -> None:
    """Sustituye los handlers del root logger por uno JSON en stdout.

    Llamar ANTES de `uvicorn.run` (server.py). Para que la salida siga redactada,
    hay que llamar después a `install_log_redaction(redactor, logger=logging.getLogger())`.
    """
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
