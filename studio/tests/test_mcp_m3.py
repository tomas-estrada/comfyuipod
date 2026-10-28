"""Tests del servidor MCP JSON-RPC 2.0 (M3-C, `mcp_server.py`): health, ciclo
initialize/tools/list/tools/call, resolución de proyecto, autenticación Bearer
con `STUDIO_API_TOKEN`, desactivación con `STUDIO_ENABLE_MCP=0`, lotes JSON-RPC,
notificaciones (202), errores de protocolo y redacción de secretos."""

from __future__ import annotations

import dataclasses
import json

from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.config import SECRET_PLACEHOLDER, StudioConfig
from comfy_studio.mcp_server import (
    BELT_TOOLS_EXPOSED,
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
)

MCP_URL = "/studio/api/mcp"


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if req_id is not None:
        payload["id"] = req_id
    return client.post(MCP_URL, json=payload)


def _call_tool(client: TestClient, name: str, arguments: dict | None = None):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})


def _tool_payload(resp) -> dict:
    body = resp.json()
    result = body["result"]
    assert result["isError"] is False
    return json.loads(result["content"][0]["text"])


def test_mcp_health_reports_state(client: TestClient) -> None:
    resp = client.get(f"{MCP_URL}/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mcp_enabled"] is True
    assert body["server"] == MCP_SERVER_NAME
    assert body["protocol_version"] == MCP_PROTOCOL_VERSION
    assert body["token_required"] is False


def test_mcp_initialize_returns_protocol_and_capabilities(client: TestClient) -> None:
    resp = _rpc(client, "initialize", {"protocolVersion": MCP_PROTOCOL_VERSION})
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == MCP_SERVER_NAME
    assert "tools" in result["capabilities"]


def test_mcp_tools_list_exposes_safe_subset(client: TestClient) -> None:
    resp = _rpc(client, "tools/list", {})
    assert resp.status_code == 200
    tools = resp.json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"list_projects", "list_assets"}.issubset(names)
    assert set(BELT_TOOLS_EXPOSED).issubset(names)
    # Herramientas sensibles del agente NO se exponen vía MCP
    assert "delegate_role" not in names
    assert "update_plan" not in names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        if tool["name"] != "list_projects":
            assert "project_id" in tool["inputSchema"]["properties"]


def test_mcp_tools_call_list_projects_and_assets(client: TestClient) -> None:
    proj = client.post("/studio/api/projects", json={"name": "Proyecto MCP"}).json()

    projects = _tool_payload(_call_tool(client, "list_projects"))
    assert any(p["id"] == proj["id"] for p in projects["projects"])

    assets = _tool_payload(_call_tool(client, "list_assets"))
    assert assets["project_id"] == proj["id"]
    assert assets["count"] == 0

    scoped = _tool_payload(
        _call_tool(client, "list_assets", {"project_id": proj["id"], "kind": "image"})
    )
    assert scoped["project_id"] == proj["id"]


def test_mcp_tools_call_belt_tool_list_templates(client: TestClient) -> None:
    client.post("/studio/api/projects", json={"name": "Proyecto Plantillas MCP"})
    payload = _tool_payload(_call_tool(client, "list_templates"))
    assert payload["count"] == 7
    ids = {t["id"] for t in payload["templates"]}
    assert "sdxl_image" in ids


def test_mcp_unknown_project_returns_is_error(client: TestClient) -> None:
    resp = _call_tool(client, "list_assets", {"project_id": "no-existe"})
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "Proyecto no encontrado" in result["content"][0]["text"]


def test_mcp_unknown_tool_returns_is_error(client: TestClient) -> None:
    client.post("/studio/api/projects", json={"name": "Proyecto Tool Desconocida"})
    resp = _call_tool(client, "herramienta_inventada")
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "no disponible" in result["content"][0]["text"]


def test_mcp_tools_call_invalid_params(client: TestClient) -> None:
    resp = _rpc(client, "tools/call", {"arguments": {}})
    error = resp.json()["error"]
    assert error["code"] == -32602


def test_mcp_unknown_method_returns_method_not_found(client: TestClient) -> None:
    resp = _rpc(client, "sampling/createMessage", {})
    error = resp.json()["error"]
    assert error["code"] == -32601


def test_mcp_invalid_jsonrpc_envelope(client: TestClient) -> None:
    resp = client.post(MCP_URL, json={"method": "ping"})
    error = resp.json()["error"]
    assert error["code"] == -32600


def test_mcp_malformed_json_returns_parse_error(client: TestClient) -> None:
    resp = client.post(
        MCP_URL,
        content=b"{not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


def test_mcp_notifications_return_202_without_body(client: TestClient) -> None:
    resp = client.post(
        MCP_URL,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    assert resp.status_code == 202
    assert resp.content == b""

    ping_notification = client.post(MCP_URL, json={"jsonrpc": "2.0", "method": "ping"})
    assert ping_notification.status_code == 202


def test_mcp_ping_request_returns_empty_result(client: TestClient) -> None:
    resp = _rpc(client, "ping", {})
    assert resp.status_code == 200
    assert resp.json()["result"] == {}


def test_mcp_batch_requests_and_empty_batch(client: TestClient) -> None:
    resp = client.post(
        MCP_URL,
        json=[
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert {item["id"] for item in body} == {1, 2}

    empty = client.post(MCP_URL, json=[])
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == -32600

    all_notifications = client.post(
        MCP_URL,
        json=[{"jsonrpc": "2.0", "method": "notifications/initialized"}],
    )
    assert all_notifications.status_code == 202


def test_mcp_get_returns_405(client: TestClient) -> None:
    resp = client.get(MCP_URL)
    assert resp.status_code == 405
    assert resp.headers.get("Allow") == "POST"


def test_mcp_requires_basic_auth_at_edge(anon_client: TestClient) -> None:
    resp = anon_client.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401
    health = anon_client.get(f"{MCP_URL}/health")
    assert health.status_code == 401


def test_mcp_bearer_token_enforced_when_configured(
    studio_config: StudioConfig,
    stub_comfyui_transport,
) -> None:
    # Sin Basic Auth de edge (en producción la termina nginx) para aislar la capa Bearer
    cfg = dataclasses.replace(
        studio_config,
        studio_api_token="tok-mcp-secreto",
        comfy_pass=None,
        require_auth=False,
    )
    app = create_app(config=cfg, comfyui_transport=stub_comfyui_transport)
    with TestClient(app) as c:
        health = c.get(f"{MCP_URL}/health").json()
        assert health["token_required"] is True

        missing = c.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert missing.status_code == 401

        wrong = c.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer tok-equivocado"},
        )
        assert wrong.status_code == 401

        ok = c.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Authorization": "Bearer tok-mcp-secreto"},
        )
        assert ok.status_code == 200
        assert ok.json()["result"] == {}


def test_mcp_disabled_returns_404(
    studio_config: StudioConfig,
    stub_comfyui_transport,
) -> None:
    cfg = dataclasses.replace(studio_config, studio_enable_mcp=False)
    app = create_app(config=cfg, comfyui_transport=stub_comfyui_transport)
    with TestClient(app) as c:
        c.auth = ("admin", "changeme-test-pass-42")
        resp = c.post(MCP_URL, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert resp.status_code == 404
        assert "STUDIO_ENABLE_MCP" in resp.json()["detail"]


def test_mcp_redacts_secrets_in_error_messages(client: TestClient) -> None:
    client.post("/studio/api/projects", json={"name": "Proyecto Redaccion MCP"})
    secret = "changeme-test-pass-42"
    resp = _call_tool(client, f"no-existe-{secret}")
    text = resp.json()["result"]["content"][0]["text"]
    assert secret not in text
    assert SECRET_PLACEHOLDER in text
