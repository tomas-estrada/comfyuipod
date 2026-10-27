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


def test_panel_chat_sse_consumes_delta_tokens_before_done(
    client: TestClient,
) -> None:
    """M2-SCR-012: el servidor emite token.delta y panel/app.js acumula delta antes de done."""
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Delta Live", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    stream_resp = client.post(
        f"/studio/api/projects/{project_id}/chat",
        json={
            "message": "Hola agente, responde en streaming",
            "stream": True,
        },
    )
    assert stream_resp.status_code == 200
    sse_events = _parse_sse_events(stream_resp.text)

    # Verificar que los eventos token llegan antes de done y usan la clave 'delta'
    done_index = next(i for i, (evt, _) in enumerate(sse_events) if evt == "done")
    token_events_before_done = [
        payload for evt, payload in sse_events[:done_index] if evt == "token"
    ]
    assert len(token_events_before_done) >= 2
    for tok in token_events_before_done:
        assert "delta" in tok and isinstance(tok["delta"], str) and len(tok["delta"]) > 0
        assert "text" not in tok

    # Verificar que panel/app.js lee payload.delta al procesar el evento token
    app_js = (PANEL_DIR / "app.js").read_text(encoding="utf-8")
    token_branch_start = app_js.index('if (evtName === "token")')
    token_branch_end = app_js.index('else if (evtName === "tool_call")', token_branch_start)
    token_branch = app_js[token_branch_start:token_branch_end]
    assert "payload.delta" in token_branch, (
        "panel/app.js debe leer payload.delta en el bloque evtName === 'token'"
    )

    # Simular la acumulación previa al evento done con el payload real del servidor
    accumulated_before_done = "".join(
        tok.get("delta") if isinstance(tok.get("delta"), str) else (tok.get("text") or "")
        for tok in token_events_before_done
    )
    done_payload = sse_events[done_index][1]
    assert accumulated_before_done == done_payload["reply"]
    assert len(accumulated_before_done) > 0


def test_panel_clears_selected_keyframes_across_projects(
    client: TestClient,
) -> None:
    """M2-SCR-013: cambiar de proyecto limpia selectedKeyframeIds y no filtra IDs."""
    proj_a = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto A Con Keyframe", "aspect_ratio": "16:9"},
    ).json()
    proj_b = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto B Sin Keyframes", "aspect_ratio": "16:9"},
    ).json()

    # Subir imagen keyframe en Proyecto A
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    up_a = client.post(
        f"/studio/api/projects/{proj_a['id']}/assets",
        files={"file": ("keyframe_a.png", png_bytes, "image/png")},
        data={"category": "keyframes"},
    )
    assert up_a.status_code == 201
    asset_a_id = up_a.json()["id"]

    # En Proyecto A el chat con asset_a_id funciona
    chat_a = client.post(
        f"/studio/api/projects/{proj_a['id']}/chat",
        json={
            "message": "Analiza este keyframe en A",
            "asset_ids": [asset_a_id],
            "stream": False,
        },
    )
    assert chat_a.status_code == 200

    # Si se filtrara asset_a_id a Proyecto B, el servidor rechazaría el turno (400 ValueError)
    leaked_chat_b = client.post(
        f"/studio/api/projects/{proj_b['id']}/chat",
        json={"message": "Hola en B", "asset_ids": [asset_a_id], "stream": False},
    )
    assert leaked_chat_b.status_code == 400

    # Verificar que selectProject en panel/app.js limpia y acota selectedKeyframeIds
    app_js = (PANEL_DIR / "app.js").read_text(encoding="utf-8")
    select_fn_start = app_js.index("async function selectProject(")
    select_fn_end = app_js.index("function renderActiveProjectDetail(", select_fn_start)
    select_fn_body = app_js[select_fn_start:select_fn_end]
    has_clear = "selectedKeyframeIds.clear()" in select_fn_body
    has_delete = "selectedKeyframeIds.delete(" in select_fn_body
    assert has_clear and has_delete, (
        "selectProject debe limpiar/acotar state.selectedKeyframeIds al cambiar de proyecto"
    )


def test_panel_wan_flf2v_launcher_uses_registered_first_and_last_frame_params(
    client: TestClient,
) -> None:
    """M2-SCR-014: Jobs usa first_frame_asset_id/last_frame_asset_id y expone selector final."""
    html = (PANEL_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (PANEL_DIR / "app.js").read_text(encoding="utf-8")

    # 1. El DOM de Jobs incluye tanto el selector principal como el de último fotograma
    assert 'id="template-asset-select"' in html
    assert 'id="template-last-frame-select"' in html

    # 2. app.js nunca emite start_image_asset_id y usa first_frame_asset_id + last_frame_asset_id
    assert "start_image_asset_id" not in app_js
    submit_fn_start = app_js.index("async function submitTemplateJob(")
    submit_fn_end = app_js.index("async function refreshGallery(", submit_fn_start)
    submit_fn_body = app_js[submit_fn_start:submit_fn_end]
    assert "first_frame_asset_id" in submit_fn_body
    assert "last_frame_asset_id" in submit_fn_body
    assert "template-last-frame-select" in app_js

    # 3. Verificar que wan_flf2v con ambos fotogramas crea el job (200, sin 422)
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto FLF2V Panel", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]

    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
        b"\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    first_up = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("inicio.png", png_bytes, "image/png")},
        data={"category": "uploads"},
    )
    last_up = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("final.png", png_bytes, "image/png")},
        data={"category": "uploads"},
    )
    assert first_up.status_code == 201
    assert last_up.status_code == 201
    first_asset = first_up.json()
    last_asset = last_up.json()

    submit_resp = client.post(
        "/studio/api/templates/wan_flf2v/submit",
        json={
            "project_id": project_id,
            "params": {
                "prompt": "Transición suave del plano inicial al plano final",
                "steps": 4,
                "first_frame_asset_id": first_asset["id"],
                "last_frame_asset_id": last_asset["id"],
            },
        },
    )
    assert submit_resp.status_code == 200, submit_resp.text
    result = submit_resp.json()
    assert result["status"] == "done"
    assert result["template_id"] == "wan_flf2v"
    # Ambos nodos LoadImage (8 y 9) deben resolver a los assets subidos del proyecto
    wf = result["workflow"]
    assert wf["8"]["inputs"]["image"] != "first_frame.png"
    assert wf["9"]["inputs"]["image"] != "last_frame.png"
    assert first_asset["filename"] in wf["8"]["inputs"]["image"]
    assert last_asset["filename"] in wf["9"]["inputs"]["image"]

