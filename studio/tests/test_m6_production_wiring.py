"""Tests del wiring del servicio de producción (rutas HTTP + tools MCP).

Verifica SIN GPU (colaboradores stub por defecto) el cableado de la fase 17:

  - rutas HTTP públicas: plan -> awaiting_approval, get, status, approve corre
    el lazo caro y llega a `done`;
  - la puerta de gasto: approve de un id inexistente da 404;
  - GOBERNANZA (M6-AGT-001): el agente (MCP) puede planear/leer/estado pero
    `approve_plan` NO está en el catálogo MCP ni es invocable por el agente.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

MCP_URL = "/studio/api/mcp"


def _rpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    payload: dict = {"jsonrpc": "2.0", "method": method, "id": req_id}
    if params is not None:
        payload["params"] = params
    return client.post(MCP_URL, json=payload)


def _call_tool(client: TestClient, name: str, arguments: dict | None = None):
    return _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})


# --------------------------------------------------------------------------
# Rutas HTTP públicas (accionadas por el humano)
# --------------------------------------------------------------------------


def test_plan_leaves_production_awaiting_approval(client: TestClient) -> None:
    resp = client.post(
        "/studio/api/productions",
        json={"idea": "un dron sobre la ciudad de noche", "cost_cap_usd": 2.0},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["phase"] == "awaiting_approval"
    assert body["production_id"]
    assert body["plan"]["sequences"]


def test_get_and_status_routes(client: TestClient) -> None:
    pid = client.post(
        "/studio/api/productions", json={"idea": "gato skater"}
    ).json()["production_id"]

    got = client.get(f"/studio/api/productions/{pid}")
    assert got.status_code == 200
    assert got.json()["plan"]["id"] == pid

    status = client.get(f"/studio/api/productions/{pid}/status")
    assert status.status_code == 200
    assert status.json()["phase"] == "awaiting_approval"


def test_approve_runs_the_expensive_loop_to_done(client: TestClient) -> None:
    pid = client.post(
        "/studio/api/productions", json={"idea": "playa al amanecer"}
    ).json()["production_id"]

    resp = client.post(f"/studio/api/productions/{pid}/approve")
    assert resp.status_code == 200
    body = resp.json()
    # con stubs deterministas el lazo llega a done con un reel.
    assert body["phase"] == "done"
    assert body["final_output_path"]


def test_approve_unknown_production_is_404(client: TestClient) -> None:
    resp = client.post("/studio/api/productions/prod_nope/approve")
    assert resp.status_code == 404


# --------------------------------------------------------------------------
# GOBERNANZA MCP (M6-AGT-001): el agente NUNCA aprueba
# --------------------------------------------------------------------------


def test_agent_can_plan_and_read_via_mcp(client: TestClient) -> None:
    # plan_production vía MCP deja la producción esperando aprobación.
    resp = _call_tool(client, "plan_production", {"idea": "reel de prueba MCP"})
    result = resp.json()["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    pid = payload["production_id"]
    assert payload["phase"] == "awaiting_approval"

    # get_plan y production_status también funcionan para el agente.
    got = _call_tool(client, "get_plan", {"production_id": pid})
    assert got.json()["result"]["isError"] is False
    st = _call_tool(client, "production_status", {"production_id": pid})
    assert st.json()["result"]["isError"] is False


def test_agent_cannot_approve_via_mcp(client: TestClient) -> None:
    # la puerta de gasto humano NO es invocable por el agente: approve_plan no
    # existe en el catálogo -> "no disponible".
    pid = _call_tool(client, "plan_production", {"idea": "intento de auto-aprobar"})
    payload = json.loads(pid.json()["result"]["content"][0]["text"])
    production_id = payload["production_id"]

    resp = _call_tool(client, "approve_plan", {"production_id": production_id})
    result = resp.json()["result"]
    assert result["isError"] is True
    assert "no disponible" in result["content"][0]["text"]

    # y tras el intento fallido, la producción sigue SIN aprobar.
    st = _call_tool(client, "production_status", {"production_id": production_id})
    st_payload = json.loads(st.json()["result"]["content"][0]["text"])
    assert st_payload["storyboard_approved"] is False
