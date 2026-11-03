"""M7-OPS-005: request IDs (X-Request-Id) y logs JSON opcionales (STUDIO_JSON_LOGS)."""

from __future__ import annotations

import json
import logging
import re

from fastapi.testclient import TestClient

from comfy_studio.observability import (
    JsonLogFormatter,
    bind_request_id,
    current_request_id,
    json_logs_enabled,
    unbind_request_id,
)

_RID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def test_health_response_carries_generated_request_id(client: TestClient) -> None:
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200, resp.text
    rid = resp.headers.get("X-Request-Id")
    assert rid is not None and _RID_RE.match(rid)


def test_incoming_request_id_is_echoed(client: TestClient) -> None:
    resp = client.get("/studio/api/health", headers={"X-Request-Id": "req-abc-123"})
    assert resp.headers["X-Request-Id"] == "req-abc-123"


def test_unsafe_incoming_request_id_is_replaced(client: TestClient) -> None:
    resp = client.get("/studio/api/health", headers={"X-Request-Id": "bad id <script>"})
    rid = resp.headers["X-Request-Id"]
    assert rid != "bad id <script>"
    assert _RID_RE.match(rid)


def test_request_id_present_even_on_401(anon_client: TestClient) -> None:
    resp = anon_client.get("/studio/api/health")
    assert resp.status_code == 401
    assert _RID_RE.match(resp.headers.get("X-Request-Id", ""))


def test_json_formatter_includes_context_request_id() -> None:
    record = logging.LogRecord(
        "comfy_studio.test", logging.INFO, __file__, 10, "hola %s", ("mundo",), None
    )
    assert current_request_id() == "-"
    token = bind_request_id("req-42")
    try:
        line = JsonLogFormatter().format(record)
    finally:
        unbind_request_id(token)
    payload = json.loads(line)
    assert payload["request_id"] == "req-42"
    assert payload["msg"] == "hola mundo"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "comfy_studio.test"
    assert "ts" in payload
    assert current_request_id() == "-"


def test_json_logs_enabled_env_parsing() -> None:
    assert json_logs_enabled({}) is False
    assert json_logs_enabled({"STUDIO_JSON_LOGS": "1"}) is True
    assert json_logs_enabled({"STUDIO_JSON_LOGS": "true"}) is True
    assert json_logs_enabled({"STUDIO_JSON_LOGS": "0"}) is False
