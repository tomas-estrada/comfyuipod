"""Tests para feature panel-web (VAL-SERVER-008 y VAL-CROSS-001)."""

from __future__ import annotations

import json
import subprocess
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


def _run_panel_dom_harness(scenario_payload: dict) -> dict:
    """Ejecuta panel/app.js real en Node con DOM real de elementos y devuelve evidencia DOM."""
    harness_js = r"""
const fs = require("fs");
const vm = require("vm");

const payload = JSON.parse(fs.readFileSync(0, "utf-8"));
const appJsCode = fs.readFileSync(payload.appJsPath, "utf-8");

function decodeEntities(str) {
  return String(str || "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#039;/g, "'")
    .replace(/&amp;/g, "&");
}

class DomElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName || "div").toUpperCase();
    this.ownerDocument = ownerDocument;
    this.id = "";
    this.className = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.scrollTop = 0;
    this.scrollHeight = 100;
    this.style = {};
    this.attributes = new Map();
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this._textContent = "";
    const self = this;
    this.classList = {
      _set() {
        return new Set(String(self.className || "").split(/\s+/).filter(Boolean));
      },
      add(...cls) {
        const s = this._set();
        cls.forEach((c) => s.add(c));
        self.className = Array.from(s).join(" ");
      },
      remove(...cls) {
        const s = this._set();
        cls.forEach((c) => s.delete(c));
        self.className = Array.from(s).join(" ");
      },
      toggle(c, force) {
        const s = this._set();
        const shouldAdd = force === undefined ? !s.has(c) : Boolean(force);
        if (shouldAdd) s.add(c);
        else s.delete(c);
        self.className = Array.from(s).join(" ");
      },
      contains(c) {
        return this._set().has(c);
      },
    };
  }

  setAttribute(name, val) {
    const strVal = String(val);
    this.attributes.set(name, strVal);
    if (name === "id") {
      this.id = strVal;
      if (this.ownerDocument) this.ownerDocument.registerId(this);
    } else if (name === "class") {
      this.className = strVal;
    } else if (name === "value") {
      this.value = strVal;
    } else if (name === "hidden") {
      this.hidden = true;
    }
  }

  getAttribute(name) {
    if (name === "id") return this.id || null;
    if (name === "class") return this.className || null;
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.getAttribute(name) !== null;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "hidden") this.hidden = false;
  }

  appendChild(child) {
    if (child.parentNode) {
      child.parentNode.removeChild(child);
    }
    child.parentNode = this;
    this.children.push(child);
    if (this.ownerDocument) this.ownerDocument.registerSubtree(child);
    return child;
  }

  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) {
      this.children.splice(idx, 1);
      child.parentNode = null;
    }
  }

  remove() {
    if (this.parentNode) {
      this.parentNode.removeChild(this);
    }
  }

  contains(node) {
    if (!node) return false;
    if (node === this) return true;
    for (const ch of this.children) {
      if (ch.contains(node)) return true;
    }
    return false;
  }

  get textContent() {
    if (this.children.length === 0) return this._textContent;
    return this.children.map((c) => c.textContent).join("");
  }

  set textContent(val) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._textContent = String(val ?? "");
  }

  get innerHTML() {
    if (this.children.length === 0) return this._textContent;
    return this.children.map((c) => c.outerHTML).join("");
  }

  set innerHTML(html) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._textContent = "";
    const parsed = parseHtmlFragment(String(html ?? ""), this.ownerDocument);
    parsed.forEach((el) => this.appendChild(el));
  }

  get outerHTML() {
    const attrs = [];
    if (this.id) attrs.push(`id="${this.id}"`);
    if (this.className) attrs.push(`class="${this.className}"`);
    for (const [k, v] of this.attributes.entries()) {
      if (k !== "id" && k !== "class") attrs.push(`${k}="${v}"`);
    }
    const attrStr = attrs.length ? " " + attrs.join(" ") : "";
    const tag = this.tagName.toLowerCase();
    return `<${tag}${attrStr}>${this.innerHTML}</${tag}>`;
  }

  matches(selector) {
    const sel = selector.trim();
    if (!sel) return false;
    if (sel.startsWith("#")) return this.id === sel.slice(1);
    const attrRegex = /\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]/g;
    let base = sel.replace(attrRegex, "").trim();
    if (base) {
      if (base.startsWith(".")) {
        const reqClasses = base.slice(1).split(".").filter(Boolean);
        for (const rc of reqClasses) {
          if (!this.classList.contains(rc)) return false;
        }
      } else {
        const parts = base.split(".");
        const tagPart = parts[0];
        if (tagPart && this.tagName !== tagPart.toUpperCase()) return false;
        for (const rc of parts.slice(1)) {
          if (!this.classList.contains(rc)) return false;
        }
      }
    }
    let m;
    while ((m = attrRegex.exec(sel)) !== null) {
      const attrName = m[1];
      const attrVal = m[2];
      const actual = this.getAttribute(attrName);
      if (actual === null) return false;
      if (attrVal !== undefined && actual !== attrVal) return false;
    }
    return true;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      for (const ch of node.children) {
        if (ch.matches(selector)) out.push(ch);
        walk(ch);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    let cur = this;
    while (cur && cur instanceof DomElement) {
      if (cur.matches(selector)) return cur;
      cur = cur.parentNode;
    }
    return null;
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }

  async dispatchAsync(type, eventInit = {}) {
    const evt = {
      type,
      target: eventInit.target || this,
      currentTarget: this,
      preventDefault() { this.defaultPrevented = true; },
      ...eventInit,
    };
    let cur = this;
    while (cur) {
      const handlers = (cur.listeners && cur.listeners.get(type)) || [];
      for (const h of handlers) {
        await h(evt);
      }
      cur = cur.parentNode || (cur !== this.ownerDocument ? this.ownerDocument : null);
    }
  }
}

function parseHtmlFragment(html, doc) {
  const tokenRegex = /<\/?([a-zA-Z0-9-]+)([^>]*)>|([^<]+)/g;
  const root = new DomElement("fragment", doc);
  const stack = [root];
  const voidTags = new Set(["IMG", "INPUT", "BR", "HR", "META", "LINK"]);
  let match;
  while ((match = tokenRegex.exec(html)) !== null) {
    const full = match[0];
    if (match[3] !== undefined) {
      const text = decodeEntities(match[3]);
      if (text.trim()) {
        const top = stack[stack.length - 1];
        if (top.children.length === 0) {
          top._textContent += text.trim();
        } else {
          const span = new DomElement("span", doc);
          span._textContent = text.trim();
          top.appendChild(span);
        }
      }
      continue;
    }
    const isClosing = full.startsWith("</");
    const tag = match[1];
    const attrChunk = match[2] || "";
    if (isClosing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new DomElement(tag, doc);
    const attrRx = /([a-zA-Z0-9_-]+)(?:="([^"]*)")?/g;
    let am;
    while ((am = attrRx.exec(attrChunk)) !== null) {
      el.setAttribute(am[1], decodeEntities(am[2] ?? ""));
    }
    stack[stack.length - 1].appendChild(el);
    if (!voidTags.has(el.tagName) && !attrChunk.trim().endsWith("/")) {
      stack.push(el);
    }
  }
  return Array.from(root.children);
}

class DomDocument {
  constructor() {
    this.byId = new Map();
    this.listeners = new Map();
    this.body = new DomElement("body", this);
  }
  createElement(tag) {
    return new DomElement(tag, this);
  }
  registerId(el) {
    if (el.id) this.byId.set(el.id, el);
  }
  registerSubtree(el) {
    if (el.id) this.byId.set(el.id, el);
    for (const ch of el.children) this.registerSubtree(ch);
  }
  getElementById(id) {
    return this.byId.get(id) || null;
  }
  querySelectorAll(sel) {
    return this.body.querySelectorAll(sel);
  }
  querySelector(sel) {
    return this.body.querySelector(sel);
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  async dispatchDOMContentLoaded() {
    const handlers = this.listeners.get("DOMContentLoaded") || [];
    for (const h of handlers) {
      await h({ type: "DOMContentLoaded" });
    }
  }
}

const document = new DomDocument();
const elementSpecs = [
  ["div", "active-project-badge"],
  ["h2", "active-project-title"],
  ["code", "active-project-id-code"],
  ["span", "active-project-summary"],
  ["div", "projects-list"],
  ["div", "project-assets-list"],
  ["span", "project-assets-count"],
  ["div", "chat-keyframes-selector"],
  ["div", "chat-messages-list"],
  ["span", "chat-stream-status"],
  ["button", "chat-send-btn"],
  ["input", "chat-stream-checkbox", { checked: true }],
  ["form", "chat-form"],
  ["textarea", "chat-message-input"],
  ["div", "chat-tool-events"],
  ["span", "tool-events-count"],
  ["button", "clear-chat-btn"],
  ["div", "templates-list"],
  ["div", "jobs-list"],
  ["div", "gallery-grid"],
  ["select", "gallery-category-filter", { value: "all" }],
  ["div", "toast-container"],
  ["div", "studio-toast"],
];
for (const [tag, id, extra] of elementSpecs) {
  const el = document.createElement(tag);
  el.setAttribute("id", id);
  if (extra) Object.assign(el, extra);
  document.body.appendChild(el);
}

const projectsById = {};
for (const p of payload.projects) {
  projectsById[p.id] = JSON.parse(JSON.stringify(p));
}
const chatsByProject = {};
for (const [pid, msgs] of Object.entries(payload.initialChats)) {
  chatsByProject[pid] = JSON.parse(JSON.stringify(msgs));
}

const chatPostLog = [];
const chatGetLog = [];
const projectCreateLog = [];
let pendingGateResolve = null;
const pendingStreamStarted = new Promise((res) => {
  global.__notifyStreamStarted = res;
});
const pendingStreamGate = new Promise((res) => {
  pendingGateResolve = res;
});
let pendingBDetailGateResolve = null;
let bDetailHeldOnce = false;
const pendingBDetailStarted = new Promise((res) => {
  global.__notifyBDetailStarted = res;
});
const pendingBDetailGate = new Promise((res) => {
  pendingBDetailGateResolve = res;
});

function makeJsonResponse(status, obj) {
  const text = JSON.stringify(obj);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    async json() { return JSON.parse(text); },
    async text() { return text; },
  };
}

function makeSseResponse(chunksPromiseFactory) {
  const encoder = new TextEncoder();
  let step = 0;
  let chunks = null;
  return {
    ok: true,
    status: 200,
    headers: { get: () => "text/event-stream" },
    body: {
      getReader() {
        return {
          async read() {
            if (!chunks) {
              chunks = await chunksPromiseFactory();
            }
            if (step >= chunks.length) {
              return { done: true, value: undefined };
            }
            const chunk = chunks[step++];
            return { done: false, value: encoder.encode(chunk) };
          },
        };
      },
    },
  };
}

async function fetchImpl(url, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const u = new URL(url, "http://127.0.0.1:39171");
  const path = u.pathname;

  if (method === "GET" && path === "/studio/api/health") {
    return makeJsonResponse(200, { status: "ok", llm_mode: "stub", comfyui_reachable: true });
  }
  if (method === "GET" && path === "/studio/api/templates") {
    return makeJsonResponse(200, { templates: [] });
  }
  if (method === "GET" && path === "/studio/api/projects") {
    return makeJsonResponse(200, { projects: Object.values(projectsById) });
  }
  if (method === "POST" && path === "/studio/api/projects") {
    const body = JSON.parse(options.body || "{}");
    const newId = `proj-auto-${projectCreateLog.length + 1}`;
    const created = {
      id: newId,
      name: body.name || "Proyecto",
      aspect_ratio: body.aspect_ratio || "16:9",
      description: body.description || "",
      assets: [],
      jobs: [],
    };
    projectsById[newId] = created;
    projectCreateLog.push(JSON.parse(JSON.stringify(created)));
    return makeJsonResponse(200, created);
  }
  const projMatch = path.match(/^\/studio\/api\/projects\/([^/]+)$/);
  if (method === "DELETE" && projMatch) {
    const pid = decodeURIComponent(projMatch[1]);
    if (!projectsById[pid]) {
      return makeJsonResponse(404, { detail: "Proyecto no encontrado" });
    }
    delete projectsById[pid];
    delete chatsByProject[pid];
    return makeJsonResponse(200, { deleted: pid });
  }
  if (method === "GET" && projMatch) {
    const pid = decodeURIComponent(projMatch[1]);
    if (payload.holdProjectBDetailGet && pid === payload.projectBId && !bDetailHeldOnce) {
      bDetailHeldOnce = true;
      global.__notifyBDetailStarted();
      await pendingBDetailGate;
    }
    return makeJsonResponse(200, projectsById[pid]);
  }
  const chatMatch = path.match(/^\/studio\/api\/projects\/([^/]+)\/chat$/);
  if (chatMatch) {
    const pid = decodeURIComponent(chatMatch[1]);
    if (method === "GET") {
      chatGetLog.push(pid);
      return makeJsonResponse(200, { project_id: pid, messages: chatsByProject[pid] || [] });
    }
    if (method === "POST") {
      const body = JSON.parse(options.body || "{}");
      chatPostLog.push({ project_id: pid, message: body.message });
      if (!projectsById[pid]) {
        return makeJsonResponse(404, { detail: "Proyecto no encontrado" });
      }
      if (chatPostLog.length === 1 && payload.holdFirstTurn) {
        global.__notifyStreamStarted();
        return makeSseResponse(async () => {
          await pendingStreamGate;
          const replyText = `Respuesta diferida para ${pid}: ${body.message}`;
          chatsByProject[pid] = (chatsByProject[pid] || []).concat([
            { role: "user", content: body.message },
            { role: "assistant", content: replyText },
          ]);
          const donePayload = { reply: replyText, tool_calls: [], tool_results: [] };
          return [
            `event: token\ndata: ${JSON.stringify({ text: replyText, iteration: 0 })}\n\n`,
            `event: done\ndata: ${JSON.stringify(donePayload)}\n\n`,
          ];
        });
      }
      const replyText = `Respuesta inmediata para ${pid}: ${body.message}`;
      chatsByProject[pid] = (chatsByProject[pid] || []).concat([
        { role: "user", content: body.message },
        { role: "assistant", content: replyText },
      ]);
      const donePayload = { reply: replyText, tool_calls: [], tool_results: [] };
      return makeSseResponse(async () => [
        `event: token\ndata: ${JSON.stringify({ text: replyText, iteration: 0 })}\n\n`,
        `event: done\ndata: ${JSON.stringify(donePayload)}\n\n`,
      ]);
    }
  }
  const galMatch = path.match(/^\/studio\/api\/projects\/([^/]+)\/gallery$/);
  if (method === "GET" && galMatch) {
    return makeJsonResponse(200, { items: [] });
  }
  if (method === "GET" && path === "/studio/api/jobs") {
    return makeJsonResponse(200, { jobs: [] });
  }
  return makeJsonResponse(404, { detail: "not found" });
}

function getVisibleMessages() {
  const listEl = document.getElementById("chat-messages-list");
  return listEl.querySelectorAll(".chat-msg-text").map((el) => el.textContent.trim());
}

function snapshotDom() {
  const listEl = document.getElementById("chat-messages-list");
  return {
    activeBadgeText: document.getElementById("active-project-badge").textContent.trim(),
    activeProjectTitle: document.getElementById("active-project-title").textContent.trim(),
    activeProjectIdCode: document.getElementById("active-project-id-code").textContent.trim(),
    chatDomProjectIdAttr: listEl.getAttribute("data-chat-project-id"),
    visibleMessages: getVisibleMessages(),
    sendBtnDisabled: Boolean(document.getElementById("chat-send-btn").disabled),
    toastText: (document.getElementById("studio-toast").textContent || "").trim(),
    toastClass: document.getElementById("studio-toast").className,
  };
}

async function flushAsync(ticks = 25) {
  for (let i = 0; i < ticks; i++) {
    await new Promise((r) => setImmediate(r));
  }
}

async function selectProjectInDomNoWait(projectId) {
  const listRoot = document.getElementById("projects-list");
  const btns = listRoot.querySelectorAll('[data-action="select-project"]');
  const targetBtn = btns.find((b) => b.getAttribute("data-project-id") === projectId);
  if (!targetBtn) {
    throw new Error(`No select-project button found for ${projectId}`);
  }
  return targetBtn.dispatchAsync("click", { target: targetBtn });
}

async function selectProjectInDom(projectId) {
  await selectProjectInDomNoWait(projectId);
  await flushAsync();
}

async function submitChatMessageInDom(text) {
  const input = document.getElementById("chat-message-input");
  const form = document.getElementById("chat-form");
  input.value = text;
  await form.dispatchAsync("submit", { target: form });
}

(async () => {
  document.readyState = "loading";
  const win = {
    location: { origin: "http://127.0.0.1:39171" },
    document,
    fetch: fetchImpl,
  };
  const runApp = new Function("window", "document", "fetch", appJsCode);
  runApp(win, document, fetchImpl);
  await document.dispatchDOMContentLoaded();
  await flushAsync();

  const initialSnap = snapshotDom();

  if (payload.scenario === "delete-last-project") {
    const listRoot = document.getElementById("projects-list");
    const delBtn = listRoot
      .querySelectorAll('[data-action="delete-project"]')
      .find((b) => b.getAttribute("data-project-id") === payload.projectAId);
    if (!delBtn) {
      throw new Error(`No delete-project button found for ${payload.projectAId}`);
    }
    await delBtn.dispatchAsync("click", { target: delBtn });
    await flushAsync();
    const afterDeleteSnap = snapshotDom();

    await submitChatMessageInDom(payload.messageAfterDelete);
    await flushAsync();
    const afterChatSnap = snapshotDom();

    process.stdout.write(JSON.stringify({
      afterInitSnap: initialSnap,
      afterDeleteSnap,
      afterChatSnap,
      chatPostLog,
      chatGetLog,
      projectCreateLog,
      projectsById,
    }));
    return;
  }

  // Iniciar turno pendiente en proyecto A (sin await aún para simular vuelo asíncrono)
  const firstTurnPromise = submitChatMessageInDom(payload.firstMessageInA);
  await pendingStreamStarted;
  await flushAsync(5);
  const duringPendingInASnap = snapshotDom();

  if (payload.holdProjectBDetailGet) {
    const selectBPromise = selectProjectInDomNoWait(payload.projectBId);
    await pendingBDetailStarted;
    await flushAsync(5);
    const whileBLoadingSnap = snapshotDom();

    let bSubmitPromise = null;
    let afterSubmitWhileBLoadingSnap = null;
    if (payload.messageWhileBLoading) {
      bSubmitPromise = submitChatMessageInDom(payload.messageWhileBLoading);
      await flushAsync(5);
      afterSubmitWhileBLoadingSnap = snapshotDom();
    }
    const postsBeforeReleaseBDetail = JSON.parse(JSON.stringify(chatPostLog));

    let afterReturningToASnap = null;
    if (payload.returnToAWhileBLoading) {
      await selectProjectInDom(payload.projectAId);
      afterReturningToASnap = snapshotDom();
    }

    pendingBDetailGateResolve();
    await selectBPromise;
    if (bSubmitPromise) {
      await bSubmitPromise;
    }
    await flushAsync();
    const afterBSubmissionResumesSnap = snapshotDom();

    pendingGateResolve();
    await firstTurnPromise;
    await flushAsync();
    const afterAFinishedSnap = snapshotDom();

    let afterFinalSelectBSnap = null;
    if (payload.selectBAfterAll) {
      await selectProjectInDom(payload.projectBId);
      afterFinalSelectBSnap = snapshotDom();
    }

    process.stdout.write(JSON.stringify({
      initialSnap,
      duringPendingInASnap,
      whileBLoadingSnap,
      afterSubmitWhileBLoadingSnap,
      postsBeforeReleaseBDetail,
      afterReturningToASnap,
      afterBSubmissionResumesSnap,
      afterAFinishedSnap,
      afterFinalSelectBSnap,
      chatPostLog,
      chatGetLog,
      chatsByProject,
    }));
    return;
  }

  // Navegar a proyecto B mientras el turno de A sigue pendiente
  await selectProjectInDom(payload.projectBId);
  const afterSwitchToBSnap = snapshotDom();

  if (payload.switchBackToABeforeFinish) {
    await selectProjectInDom(payload.projectAId);
  }
  const beforeReleaseSnap = snapshotDom();

  // Liberar el stream SSE de A y esperar su finalización completa
  pendingGateResolve();
  await firstTurnPromise;
  await flushAsync();
  const afterAFinishedSnap = snapshotDom();

  let afterSecondMessageSnap = null;
  if (payload.secondMessageInVisibleChat) {
    await submitChatMessageInDom(payload.secondMessageInVisibleChat);
    await flushAsync();
    afterSecondMessageSnap = snapshotDom();
  }

  process.stdout.write(JSON.stringify({
    initialSnap,
    duringPendingInASnap,
    afterSwitchToBSnap,
    beforeReleaseSnap,
    afterAFinishedSnap,
    afterSecondMessageSnap,
    chatPostLog,
    chatGetLog,
    chatsByProject,
  }));
})().catch((err) => {
  console.error(err);
  process.exit(1);
});
"""
    proc = subprocess.run(
        ["node", "-e", harness_js],
        input=json.dumps(scenario_payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Node harness failed:\n{proc.stderr}")
    return json.loads(proc.stdout)


def test_panel_chat_pending_turn_switch_to_b_keeps_b_active_and_routes_next_message_to_b(
    client: TestClient,
) -> None:
    """Regresión M2-SCR-019: turno pendiente en A -> abrir B antes de que A termine ->
    la finalización de A NO debe cambiar el proyecto activo a A ni dejar el DOM de B
    desemparejado, y el siguiente mensaje enviado sobre la conversación visible debe
    publicarse en B (nunca en A)."""
    proj_b = client.post("/studio/api/projects", json={"name": "Proyecto Destino B"}).json()
    proj_a = client.post("/studio/api/projects", json={"name": "Proyecto Origen A"}).json()

    seed_b = client.post(
        f"/studio/api/projects/{proj_b['id']}/chat",
        json={"message": "Historial previo exclusivo de B", "stream": False},
    )
    assert seed_b.status_code == 200
    chat_b_initial = client.get(f"/studio/api/projects/{proj_b['id']}/chat").json()["messages"]
    chat_a_initial = client.get(f"/studio/api/projects/{proj_a['id']}/chat").json()["messages"]

    result = _run_panel_dom_harness(
        {
            "appJsPath": str(PANEL_DIR / "app.js"),
            "projects": [proj_a, proj_b],
            "initialChats": {
                proj_a["id"]: chat_a_initial,
                proj_b["id"]: chat_b_initial,
            },
            "projectAId": proj_a["id"],
            "projectBId": proj_b["id"],
            "holdFirstTurn": True,
            "firstMessageInA": "Turno en vuelo de A",
            "switchBackToABeforeFinish": False,
            "secondMessageInVisibleChat": "Mensaje siguiente en conversacion visible",
        }
    )

    # 1. Al cambiar a B durante el turno pendiente de A, B se muestra con su propio historial
    assert result["afterSwitchToBSnap"]["activeProjectIdCode"] == proj_b["id"]
    assert result["afterSwitchToBSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert any(
        "Historial previo exclusivo de B" in m
        for m in result["afterSwitchToBSnap"]["visibleMessages"]
    )
    assert all(
        "Turno en vuelo de A" not in m
        for m in result["afterSwitchToBSnap"]["visibleMessages"]
    )

    # 2. Cuando el turno de A termina en segundo plano, B DEBE permanecer activo y visible
    assert result["afterAFinishedSnap"]["activeProjectIdCode"] == proj_b["id"]
    assert result["afterAFinishedSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert "Proyecto Destino B" in result["afterAFinishedSnap"]["activeBadgeText"]
    assert any(
        "Historial previo exclusivo de B" in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )
    assert all(
        "Turno en vuelo de A" not in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )

    # 3. El siguiente mensaje escrito en la conversación mostrada se publica en B (no en A)
    assert len(result["chatPostLog"]) == 2
    assert result["chatPostLog"][0] == {
        "project_id": proj_a["id"],
        "message": "Turno en vuelo de A",
    }
    assert result["chatPostLog"][1] == {
        "project_id": proj_b["id"],
        "message": "Mensaje siguiente en conversacion visible",
    }
    assert result["afterSecondMessageSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert any(
        "Mensaje siguiente en conversacion visible" in m
        for m in result["afterSecondMessageSnap"]["visibleMessages"]
    )


def test_panel_chat_pending_turn_roundtrip_a_b_a_reloads_completed_history_of_a(
    client: TestClient,
) -> None:
    """Regresión M2-SCR-019: si el usuario navega A -> B -> A mientras el turno de A sigue
    pendiente (descartando las burbujas en vivo de A), al finalizar A no debe preservarse
    un DOM obsoleto con preserveChatDom=true sino recargarse el historial actualizado de A."""
    proj_b = client.post("/studio/api/projects", json={"name": "Proyecto Paso B"}).json()
    proj_a = client.post("/studio/api/projects", json={"name": "Proyecto Retorno A"}).json()

    result = _run_panel_dom_harness(
        {
            "appJsPath": str(PANEL_DIR / "app.js"),
            "projects": [proj_a, proj_b],
            "initialChats": {
                proj_a["id"]: [],
                proj_b["id"]: [{"role": "user", "content": "Mensaje de B"}],
            },
            "projectAId": proj_a["id"],
            "projectBId": proj_b["id"],
            "holdFirstTurn": True,
            "firstMessageInA": "Turno de A tras ida y vuelta",
            "switchBackToABeforeFinish": True,
            "secondMessageInVisibleChat": None,
        }
    )

    assert result["afterAFinishedSnap"]["activeProjectIdCode"] == proj_a["id"]
    assert result["afterAFinishedSnap"]["chatDomProjectIdAttr"] == proj_a["id"]
    assert any(
        "Turno de A tras ida y vuelta" in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )
    assert any(
        f"Respuesta diferida para {proj_a['id']}: Turno de A tras ida y vuelta" in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )


def test_panel_chat_delayed_b_selection_submission_never_routes_or_renders_in_a(
    client: TestClient,
) -> None:
    """Regresión M2-SCR-020: turno de A pendiente -> seleccionar B mientras su GET de detalle
    está demorado -> enviar mensaje mientras B posee el DOM del chat (send habilitado) ->
    volver a A antes de que el GET de detalle de B se libere -> liberar el GET de B ->
    el mensaje enviado desde la conversación de B DEBE publicarse en B (JAMÁS en A) y
    NUNCA aparecer en el DOM de A."""
    proj_b = client.post("/studio/api/projects", json={"name": "Project B"}).json()
    proj_a = client.post("/studio/api/projects", json={"name": "Project A"}).json()

    result = _run_panel_dom_harness(
        {
            "appJsPath": str(PANEL_DIR / "app.js"),
            "projects": [proj_a, proj_b],
            "initialChats": {
                proj_a["id"]: [],
                proj_b["id"]: [],
            },
            "projectAId": proj_a["id"],
            "projectBId": proj_b["id"],
            "holdFirstTurn": True,
            "firstMessageInA": "Initial pending A turn",
            "holdProjectBDetailGet": True,
            "messageWhileBLoading": "Message submitted while B conversation is displayed",
            "returnToAWhileBLoading": True,
            "selectBAfterAll": True,
        }
    )

    # 1. Mientras el GET de detalle de B sigue retenido, B posee el DOM del chat
    #    y el botón de envío está habilitado.
    assert result["whileBLoadingSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert result["whileBLoadingSnap"]["sendBtnDisabled"] is False
    assert "Project B" in result["whileBLoadingSnap"]["activeBadgeText"]

    # 2. Al enviar el mensaje mientras B posee el DOM del chat, el envío de B queda
    #    pendiente (botón deshabilitado en B).
    assert result["afterSubmitWhileBLoadingSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert result["afterSubmitWhileBLoadingSnap"]["sendBtnDisabled"] is True

    # 3. Al regresar a A antes de liberar el GET de B, A vuelve a ser el proyecto activo en el DOM
    assert result["afterReturningToASnap"]["activeProjectIdCode"] == proj_a["id"]
    assert result["afterReturningToASnap"]["chatDomProjectIdAttr"] == proj_a["id"]
    assert all(
        "Message submitted while B conversation is displayed" not in m
        for m in result["afterReturningToASnap"]["visibleMessages"]
    )

    # 4. Al liberar el GET de detalle de B y reanudarse el envío encolado de B:
    #    - el POST debe dirigirse a B (JAMÁS a A)
    #    - el mensaje de B NUNCA aparece en el DOM de A
    assert len(result["chatPostLog"]) == 2
    assert result["chatPostLog"][0] == {
        "project_id": proj_a["id"],
        "message": "Initial pending A turn",
    }
    assert result["chatPostLog"][1] == {
        "project_id": proj_b["id"],
        "message": "Message submitted while B conversation is displayed",
    }
    assert result["afterBSubmissionResumesSnap"]["activeProjectIdCode"] == proj_a["id"]
    assert result["afterBSubmissionResumesSnap"]["chatDomProjectIdAttr"] == proj_a["id"]
    assert all(
        "Message submitted while B conversation is displayed" not in m
        for m in result["afterBSubmissionResumesSnap"]["visibleMessages"]
    )

    # 5. Al finalizar también el turno original de A, A contiene solo su turno y B contiene el suyo
    assert result["afterAFinishedSnap"]["activeProjectIdCode"] == proj_a["id"]
    assert result["afterAFinishedSnap"]["chatDomProjectIdAttr"] == proj_a["id"]
    assert any(
        "Initial pending A turn" in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )
    assert all(
        "Message submitted while B conversation is displayed" not in m
        for m in result["afterAFinishedSnap"]["visibleMessages"]
    )
    assert result["afterFinalSelectBSnap"]["activeProjectIdCode"] == proj_b["id"]
    assert result["afterFinalSelectBSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert any(
        "Message submitted while B conversation is displayed" in m
        for m in result["afterFinalSelectBSnap"]["visibleMessages"]
    )
    expected_b_reply = (
        f"Respuesta inmediata para {proj_b['id']}: "
        "Message submitted while B conversation is displayed"
    )
    assert any(
        expected_b_reply in m
        for m in result["afterFinalSelectBSnap"]["visibleMessages"]
    )


def test_panel_chat_delayed_b_selection_submission_staying_on_b_preserves_history_and_new_turn(
    client: TestClient,
) -> None:
    """Regresión M2-SCR-020: si el usuario envía un mensaje en B mientras el GET de detalle
    de B está demorado y permanece en B cuando el GET se libera, el envío va a B y el DOM
    de B conserva tanto el historial previo de B como el nuevo turno completado."""
    proj_b = client.post("/studio/api/projects", json={"name": "Project B Stay"}).json()
    proj_a = client.post("/studio/api/projects", json={"name": "Project A Orig"}).json()

    result = _run_panel_dom_harness(
        {
            "appJsPath": str(PANEL_DIR / "app.js"),
            "projects": [proj_a, proj_b],
            "initialChats": {
                proj_a["id"]: [],
                proj_b["id"]: [{"role": "user", "content": "Historial previo de B"}],
            },
            "projectAId": proj_a["id"],
            "projectBId": proj_b["id"],
            "holdFirstTurn": True,
            "firstMessageInA": "Turno inicial de A",
            "holdProjectBDetailGet": True,
            "messageWhileBLoading": "Nuevo turno enviado en B durante carga",
            "returnToAWhileBLoading": False,
            "selectBAfterAll": False,
        }
    )

    assert len(result["chatPostLog"]) == 2
    assert result["chatPostLog"][1] == {
        "project_id": proj_b["id"],
        "message": "Nuevo turno enviado en B durante carga",
    }
    assert result["afterBSubmissionResumesSnap"]["activeProjectIdCode"] == proj_b["id"]
    assert result["afterBSubmissionResumesSnap"]["chatDomProjectIdAttr"] == proj_b["id"]
    assert any(
        "Historial previo de B" in m
        for m in result["afterBSubmissionResumesSnap"]["visibleMessages"]
    )
    assert any(
        "Nuevo turno enviado en B durante carga" in m
        for m in result["afterBSubmissionResumesSnap"]["visibleMessages"]
    )
    assert any(
        f"Respuesta inmediata para {proj_b['id']}: Nuevo turno enviado en B durante carga" in m
        for m in result["afterBSubmissionResumesSnap"]["visibleMessages"]
    )

def test_panel_chat_after_deleting_only_project_recovers_and_never_posts_to_deleted(
    client: TestClient,
) -> None:
    """Regresión M2-SCR-021: borrar el ÚNICO proyecto deja la propiedad de conversación
    (chatDomProjectId) huérfana; el siguiente envío NO debe publicarse en el proyecto
    borrado (404 silencioso con el input ya descartado) — el panel debe restaurar la
    recuperación de sin-proyecto (crea uno nuevo vía ensureActiveProject), entregar el
    mensaje allí y mostrar feedback visible en cada paso."""
    proj = client.post("/studio/api/projects", json={"name": "Proyecto Unico"}).json()

    result = _run_panel_dom_harness(
        {
            "appJsPath": str(PANEL_DIR / "app.js"),
            "projects": [proj],
            "initialChats": {
                proj["id"]: [
                    {"role": "user", "content": "Historial del unico proyecto"},
                ],
            },
            "projectAId": proj["id"],
            "projectBId": "",
            "scenario": "delete-last-project",
            "messageAfterDelete": "Mensaje tras borrar el unico proyecto",
            "holdFirstTurn": False,
            "firstMessageInA": "",
        }
    )

    # 1. Tras init, el proyecto único posee el DOM del chat con su historial visible
    assert result["afterInitSnap"]["chatDomProjectIdAttr"] == proj["id"]
    assert any(
        "Historial del unico proyecto" in m
        for m in result["afterInitSnap"]["visibleMessages"]
    )

    # 2. Tras borrarlo: propiedad de conversación limpiada (sin dueño huérfano),
    #    historial del proyecto borrado fuera del DOM, feedback visible del borrado
    #    y el botón de envío sigue habilitado (recuperación disponible).
    after_delete = result["afterDeleteSnap"]
    assert after_delete["chatDomProjectIdAttr"] is None
    assert after_delete["activeProjectIdCode"] == "Sin proyecto"
    assert "Proyecto: Ninguno" in after_delete["activeBadgeText"]
    assert "eliminado" in after_delete["toastText"]
    assert "toast-success" in after_delete["toastClass"]
    assert all(
        "Historial del unico proyecto" not in m
        for m in after_delete["visibleMessages"]
    )
    assert after_delete["sendBtnDisabled"] is False

    # 3. El envío posterior JAMÁS se publica en el proyecto borrado: el panel crea
    #    un proyecto nuevo (recuperación de sin-proyecto) y entrega el mensaje allí.
    assert len(result["projectCreateLog"]) == 1
    created_id = result["projectCreateLog"][0]["id"]
    assert created_id != proj["id"]
    assert result["chatPostLog"] == [
        {"project_id": created_id, "message": "Mensaje tras borrar el unico proyecto"}
    ]

    # 4. El mensaje NO se pierde: visible en el DOM del nuevo proyecto con feedback
    #    de éxito; el DOM del chat queda emparejado al nuevo proyecto activo.
    after_chat = result["afterChatSnap"]
    assert after_chat["chatDomProjectIdAttr"] == created_id
    assert after_chat["activeProjectIdCode"] == created_id
    assert any(
        "Mensaje tras borrar el unico proyecto" in m
        for m in after_chat["visibleMessages"]
    )
    assert any(
        f"Respuesta inmediata para {created_id}" in m
        for m in after_chat["visibleMessages"]
    )
    assert "completada" in after_chat["toastText"]
    assert "toast-success" in after_chat["toastClass"]



