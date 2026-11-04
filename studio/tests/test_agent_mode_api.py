"""Tests del prototipo "Modo Agente" web (docs/propuestas/PROTOTIPO_MODO_AGENTE_WEB.md).

Cubre el contrato nuevo de producciones que consume `/studio/agent.html`:
listar producciones por sesión, editar el plan solo en `awaiting_approval`,
cancelación (terminal y cooperativo durante ejecución), eventos SSE
`production_*` del broker, y las páginas estáticas del modo agente (CSP-safe).

Todo corre SIN GPU/LLM (colaboradores stub por defecto en los tests).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.conductor import build_production_graph
from comfy_studio.orchestration.service import (
    ProductionAlreadyTerminalError,
    ProductionService,
)
from comfy_studio.progress import ProgressBroker

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = REPO_ROOT / "panel"


def _plan(client: TestClient, idea: str = "dron sobre la ciudad de noche", **extra) -> str:
    body = {"idea": idea, **extra}
    resp = client.post("/studio/api/productions", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["production_id"]


def _create_project(client: TestClient, name: str = "Sesión agente") -> str:
    resp = client.post("/studio/api/projects", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Listado de producciones (sesiones de trabajo del modo agente)
# ---------------------------------------------------------------------------


def test_list_productions_empty_and_filtered_by_project(client: TestClient) -> None:
    resp = client.get("/studio/api/productions")
    assert resp.status_code == 200
    assert resp.json()["productions"] == []

    project_id = _create_project(client)
    other = _plan(client, "idea sin proyecto")
    mine = _plan(client, "idea del proyecto", project_id=project_id)

    all_prods = client.get("/studio/api/productions").json()["productions"]
    ids = {p["production_id"] for p in all_prods}
    assert {other, mine} <= ids

    filtered = client.get(
        "/studio/api/productions", params={"project_id": project_id}
    ).json()["productions"]
    assert [p["production_id"] for p in filtered] == [mine]
    assert filtered[0]["project_id"] == project_id
    assert filtered[0]["phase"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# Edición del plan (visible y editable) solo en awaiting_approval
# ---------------------------------------------------------------------------


def test_patch_plan_edits_shots_while_awaiting_approval(client: TestClient) -> None:
    pid = _plan(client)
    plan = client.get(f"/studio/api/productions/{pid}").json()["plan"]
    shot = plan["sequences"][0]["shots"][0]
    shot2 = plan["sequences"][0]["shots"][1]

    resp = client.patch(
        f"/studio/api/productions/{pid}/plan",
        json={
            "idea": "dron sobre la ciudad de noche, estilo neón",
            "shots": [
                {"id": shot["id"], "description": "apertura neón", "duration_s": 4.0},
                {"id": shot2["id"], "remove": True},
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()["plan"]
    assert plan["idea"] == "dron sobre la ciudad de noche, estilo neón"
    remaining = {s["id"]: s for seq in plan["sequences"] for s in seq["shots"]}
    assert shot["id"] in remaining
    assert remaining[shot["id"]]["description"] == "apertura neón"
    assert remaining[shot["id"]]["duration_s"] == 4.0
    assert shot2["id"] not in remaining

    # persiste: GET refleja la edición
    got = client.get(f"/studio/api/productions/{pid}").json()["plan"]
    assert got["idea"] == "dron sobre la ciudad de noche, estilo neón"


def test_patch_plan_rejects_unknown_shot_and_empty_plan(client: TestClient) -> None:
    pid = _plan(client)
    plan = client.get(f"/studio/api/productions/{pid}").json()["plan"]
    shot_ids = [s["id"] for seq in plan["sequences"] for s in seq["shots"]]

    resp = client.patch(
        f"/studio/api/productions/{pid}/plan",
        json={"shots": [{"id": "shot_inexistente", "description": "x"}]},
    )
    assert resp.status_code == 400

    resp = client.patch(
        f"/studio/api/productions/{pid}/plan",
        json={"shots": [{"id": sid, "remove": True} for sid in shot_ids]},
    )
    assert resp.status_code == 400


def test_patch_plan_after_approve_is_409(client: TestClient) -> None:
    pid = _plan(client)
    approved = client.post(f"/studio/api/productions/{pid}/approve")
    assert approved.status_code == 200
    resp = client.patch(
        f"/studio/api/productions/{pid}/plan", json={"idea": "cambio tardío"}
    )
    assert resp.status_code == 409


def test_patch_plan_unknown_production_is_404(client: TestClient) -> None:
    resp = client.patch(
        "/studio/api/productions/prod_nope/plan", json={"idea": "x"}
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cancelación
# ---------------------------------------------------------------------------


def test_cancel_awaiting_approval_is_terminal_and_blocks_approve(
    client: TestClient,
) -> None:
    pid = _plan(client)
    resp = client.post(f"/studio/api/productions/{pid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "cancelled"

    status = client.get(f"/studio/api/productions/{pid}/status").json()
    assert status["phase"] == "cancelled"

    approve = client.post(f"/studio/api/productions/{pid}/approve")
    assert approve.status_code == 409
    cancel_again = client.post(f"/studio/api/productions/{pid}/cancel")
    assert cancel_again.status_code == 409


def test_cancel_after_done_is_409(client: TestClient) -> None:
    pid = _plan(client)
    assert client.post(f"/studio/api/productions/{pid}/approve").status_code == 200
    resp = client.post(f"/studio/api/productions/{pid}/cancel")
    assert resp.status_code == 409


def test_cancel_unknown_production_is_404(client: TestClient) -> None:
    assert client.post("/studio/api/productions/prod_nope/cancel").status_code == 404


def test_cancel_during_execution_stops_graph_at_next_step(tmp_path) -> None:
    """Cancelación cooperativa: el router detiene el lazo en el siguiente paso."""
    checkpointer = ProductionCheckpointer(tmp_path / "ckpt.sqlite3")
    service = ProductionService(checkpointer=checkpointer, broker=ProgressBroker())
    result = service.plan_production("ola de dron", project_id=None)
    pid = result["production_id"]

    state = checkpointer.load(pid)
    assert state is not None
    state.phase = "executing"
    state.metadata["cancel_requested"] = True
    checkpointer.save(state)

    graph = build_production_graph(checkpointer=checkpointer)
    final = graph.run(state)
    assert final.phase == "cancelled"
    assert any(e["kind"] == "cancelled" for e in final.log)


# ---------------------------------------------------------------------------
# Eventos SSE del ciclo de vida (bus del broker)
# ---------------------------------------------------------------------------


def test_production_lifecycle_publishes_broker_events(tmp_path) -> None:
    broker = ProgressBroker()
    service = ProductionService(
        checkpointer=ProductionCheckpointer(tmp_path / "ckpt.sqlite3"),
        broker=broker,
    )
    pid = service.plan_production("spot de café")["production_id"]
    service.update_plan(pid, idea="spot de café con amigos")
    service.approve_plan(pid)
    with pytest.raises(ProductionAlreadyTerminalError):  # ya terminal: cancel rechaza
        service.cancel_production(pid)

    events = [t for t, _ in broker.recent_events()]
    assert "production_planned" in events
    assert "production_plan_updated" in events
    assert "production_approved" in events
    assert "production_step" in events  # progreso por paso del grafo
    assert "production_finished" in events

    # el evento de paso trae el estado de progreso (shots por estado)
    step = next(d for t, d in broker.recent_events() if t == "production_step")
    assert step["production_id"] == pid
    assert "shots_by_status" in step


def test_cancel_awaiting_publishes_event(tmp_path) -> None:
    broker = ProgressBroker()
    service = ProductionService(
        checkpointer=ProductionCheckpointer(tmp_path / "ckpt.sqlite3"),
        broker=broker,
    )
    pid = service.plan_production("videoclip")["production_id"]
    status = service.cancel_production(pid)
    assert status["phase"] == "cancelled"
    events = [t for t, _ in broker.recent_events()]
    assert "production_cancelled" in events


def test_publish_threadsafe_without_loop_falls_back_to_direct() -> None:
    broker = ProgressBroker()
    broker.publish_threadsafe("custom", {"ok": True})
    assert broker.recent_events()[-1][0] == "custom"


# ---------------------------------------------------------------------------
# Páginas estáticas del modo agente (CSP-safe, sin CDNs, disclaimer de marca)
# ---------------------------------------------------------------------------


def test_agent_mode_pages_exist_and_are_csp_safe() -> None:
    for name in ("agent.html", "agent.css", "agent.js"):
        assert (PANEL_DIR / name).is_file(), f"falta panel/{name}"
    html = (PANEL_DIR / "agent.html").read_text(encoding="utf-8")
    # sin scripts inline (CSP script-src 'self') y solo assets self
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html, re.I)
    assert not re.search(r'(?:src|href)="https?://', html, re.I)
    # aclaración de no afiliación con OpenCode
    assert "no está afiliado" in html.lower() or "not affiliated" in html.lower()
    assert "/studio/agent.js" in html
    assert "/studio/agent.css" in html
    js = (PANEL_DIR / "agent.js").read_text(encoding="utf-8")
    assert not re.search(r"https?://", js), "agent.js no debe referenciar URLs externas"
    assert not re.search(r"\son(click|load|error)=", js, re.I)


def test_main_panel_links_to_agent_mode() -> None:
    index_html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
    assert "/studio/agent.html" in index_html


def test_agent_mode_page_is_served(client: TestClient) -> None:
    resp = client.get("/studio/agent.html")
    assert resp.status_code == 200
    assert "Modo Agente" in resp.text
