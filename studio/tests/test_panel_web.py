"""Tests para feature panel-web (VAL-SERVER-008 y VAL-CROSS-001)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = REPO_ROOT / "panel"


def _parse_sse_events(raw_body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    current_event = "message"
    for line in raw_body.splitlines():
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            raw_data = line.split(":", 1)[1].strip()
            if raw_data:
                events.append((current_event, json.loads(raw_data)))
    return events


def test_panel_files_exist_vanilla_without_build_artifacts() -> None:
    """El directorio panel/ contiene index.html, style.css y app.js vanilla sin node_modules."""
    assert PANEL_DIR.is_dir(), "El directorio panel/ debe existir en la raíz del repo"
    assert (PANEL_DIR / "index.html").is_file()
    assert (PANEL_DIR / "style.css").is_file()
    assert (PANEL_DIR / "app.js").is_file()
    assert not (PANEL_DIR / "node_modules").exists()


def test_panel_static_files_served_at_studio_root(client: TestClient) -> None:
    """FastAPI sirve el panel estático en /studio/, /studio, /studio/style.css y /studio/app.js."""
    resp_slash = client.get("/studio/")
    assert resp_slash.status_code == 200
    assert "text/html" in resp_slash.headers.get("content-type", "")
    html = resp_slash.text

    # Verificar vistas y navegación requeridas en el DOM inicial
    for expected_id in (
        "nav-projects",
        "nav-chat",
        "nav-jobs",
        "nav-gallery",
        "nav-edl",
        "nav-login",
        "view-projects",
        "view-chat",
        "view-jobs",
        "view-gallery",
        "view-edl",
        "view-login",
    ):
        assert expected_id in html, f"Falta el elemento #{expected_id} en index.html"

    assert "/studio/style.css" in html
    assert "/studio/app.js" in html

    # /studio sin barra final también sirve o redirige limpiamente al panel
    resp_no_slash = client.get("/studio")
    assert resp_no_slash.status_code == 200
    assert "text/html" in resp_no_slash.headers.get("content-type", "")

    # Archivos estáticos CSS y JS
    resp_css = client.get("/studio/style.css")
    assert resp_css.status_code == 200
    assert "text/css" in resp_css.headers.get("content-type", "")
    assert len(resp_css.text) > 200

    resp_js = client.get("/studio/app.js")
    assert resp_js.status_code == 200
    assert "javascript" in resp_js.headers.get("content-type", "")
    assert "/studio/api/projects" in resp_js.text
    assert "/studio/api/progress" in resp_js.text

    # /favicon.ico responde 204 para evitar errores 404 en consola del navegador
    resp_fav = client.get("/favicon.ico")
    assert resp_fav.status_code == 204


def test_panel_respects_edge_basic_auth(
    anon_client: TestClient,
    client: TestClient,
) -> None:
    """Cuando la auth de edge está activa, /studio/ exige Basic-Auth (401 sin credenciales)."""
    unauth = anon_client.get("/studio/")
    assert unauth.status_code == 401
    assert "Basic" in unauth.headers.get("WWW-Authenticate", "")

    auth_ok = client.get("/studio/")
    assert auth_ok.status_code == 200


def test_stub_llm_streaming_chat_emits_tokens_and_tool_events_end_to_end(
    client: TestClient,
) -> None:
    """Contra stubs por defecto, el chat SSE emite tokens + tool_calls y crea salida en galería."""
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Flujo Panel", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    # 1. Subir un medio al proyecto
    up_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("clip_entrada.mp4", b"\x00\x00\x00\x18ftypmp42FAKEVIDEO", "video/mp4")},
        data={"category": "uploads"},
    )
    assert up_resp.status_code == 201
    uploaded_asset = up_resp.json()
    assert uploaded_asset["original_filename"] == "clip_entrada.mp4"

    # 2. Enviar mensaje de chat en modo streaming pidiendo un render con wan_t2v
    stream_resp = client.post(
        f"/studio/api/projects/{project_id}/chat",
        json={
            "message": "Genera un vídeo corto con wan_t2v de un astronauta en Marte",
            "stream": True,
        },
    )
    assert stream_resp.status_code == 200
    assert "text/event-stream" in stream_resp.headers.get("content-type", "")

    sse_events = _parse_sse_events(stream_resp.text)
    event_types = [evt for evt, _ in sse_events]
    assert "token" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "done" in event_types

    tool_calls = [payload for evt, payload in sse_events if evt == "tool_call"]
    assert any(tc.get("name") == "enqueue_render" for tc in tool_calls)

    # 3. Verificar que el job quedó registrado y completado
    jobs_resp = client.get(f"/studio/api/jobs?project_id={project_id}")
    assert jobs_resp.status_code == 200
    jobs = jobs_resp.json()["jobs"]
    assert len(jobs) >= 1
    assert jobs[0]["status"] == "done"
    assert jobs[0]["template_id"] == "wan_t2v"

    # 4. Verificar que la galería tiene la salida lista para previsualizar y descargar
    gal_resp = client.get(f"/studio/api/projects/{project_id}/gallery")
    assert gal_resp.status_code == 200
    gal_assets = gal_resp.json()["assets"]
    assert len(gal_assets) >= 1

    output_asset = gal_assets[0]
    assert output_asset["category"] == "outputs"
    assert output_asset["view_url"]
    assert output_asset["download_url"]

    view_r = client.get(output_asset["view_url"])
    assert view_r.status_code == 200
    assert len(view_r.content) > 0

    dl_r = client.get(output_asset["download_url"])
    assert dl_r.status_code == 200
    assert "attachment" in dl_r.headers.get("content-disposition", "")


def test_stub_llm_propose_edl_in_chat_populates_project_edl(
    client: TestClient,
) -> None:
    """Al pedir un EDL al agente stub, invoca propose_edl y guarda una versión EDL."""
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto EDL Panel", "aspect_ratio": "9:16"},
    ).json()
    project_id = proj["id"]

    chat_resp = client.post(
        f"/studio/api/projects/{project_id}/chat",
        json={
            "message": "Propón un EDL de highlights para el clip inicial",
            "stream": False,
        },
    )
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    assert any(tc["name"] == "propose_edl" for tc in body["tool_calls"])

    edl_resp = client.get(f"/studio/api/projects/{project_id}/edl")
    assert edl_resp.status_code == 200
    edl_data = edl_resp.json()
    assert edl_data["edl"] is not None
    assert edl_data["edl"]["version"] >= 1
    assert len(edl_data["edl"]["edl"]["cuts"]) >= 1
