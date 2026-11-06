"""Regresiones del compositor de Modo Agente (panel/agent.js).

Ejecuta el agent.js real en Node con un doble DOM m\u00EDnimo (estado + eventos) y
sin red: fetch/EventSource son dobles que registran cada petici\u00F3n y rechazan
cualquier ruta inesperada. No se exportan funciones internas ni se copia la
l\u00F3gica de producci\u00F3n: los casos entran por los listeners reales del formulario
y del textarea.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_JS = REPO_ROOT / "panel" / "agent.js"
AGENT_HTML = REPO_ROOT / "panel" / "agent.html"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node no est\u00E1 disponible")

# Doble DOM m\u00EDnimo: solo lo que agent.js usa (querySelector, createElement,
# textContent, value, disabled, focus, listeners y requestSubmit).
HARNESS = r"""
"use strict";
const fs = require("fs");
const vm = require("vm");

const agentJs = fs.readFileSync(process.env.AGENT_JS, "utf8");
const agentHtml = fs.readFileSync(process.env.AGENT_HTML, "utf8");

// --- ids reales del HTML: si el panel cambia de id, el test falla aqu\u00ED ------
const htmlIds = new Set();
for (const m of agentHtml.matchAll(/\bid="([^"]+)"/g)) htmlIds.add(m[1]);

class El {
    constructor(tag) {
        this.tagName = String(tag || "div").toUpperCase();
        this.children = [];
        this.parentNode = null;
        this.attributes = {};
        this.dataset = {};
        this.listeners = {};
        this.className = "";
        this.id = "";
        this.type = "";
        this.value = "";
        this.disabled = false;
        this.hidden = false;
        this.checked = false;
        this.scrollTop = 0;
        this.scrollHeight = 0;
        this._text = "";
        this.classList = {
            toggle: () => {},
            add: () => {},
            remove: () => {},
            contains: () => false,
        };
    }
    get textContent() {
        if (this.children.length) return this.children.map((c) => c.textContent).join("");
        return this._text;
    }
    set textContent(v) {
        this._text = String(v ?? "");
        this.children = [];
    }
    appendChild(child) {
        child.parentNode = this;
        this.children.push(child);
        return child;
    }
    removeChild(child) {
        this.children = this.children.filter((c) => c !== child);
        child.parentNode = null;
        return child;
    }
    get lastChild() {
        return this.children[this.children.length - 1] || null;
    }
    setAttribute(name, value) {
        this.attributes[name] = String(value);
        if (name === "id") this.id = String(value);
    }
    getAttribute(name) {
        const has = Object.prototype.hasOwnProperty.call(this.attributes, name);
        return has ? this.attributes[name] : null;
    }
    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    dispatch(type, ev) {
        const base = { type, defaultPrevented: false };
        base.preventDefault = function () { this.defaultPrevented = true; };
        const event = Object.assign(base, ev || {});
        for (const fn of this.listeners[type] || []) fn(event);
        return event;
    }
    requestSubmit() {
        this.dispatch("submit");
    }
    focus() {
        doc.activeElement = this;
    }
    querySelector() {
        return null;
    }
    querySelectorAll() {
        return [];
    }
    close() {}
    showModal() {}
}

const els = {};
const doc = {
    activeElement: null,
    listeners: {},
    createElement: (tag) => new El(tag),
    createTextNode: (t) => {
        const el = new El("#text");
        el.textContent = t;
        return el;
    },
    querySelector(sel) {
        if (typeof sel !== "string" || !sel.startsWith("#")) return null;
        const id = sel.slice(1);
        if (!htmlIds.has(id)) throw new Error("selector inexistente en agent.html: " + sel);
        if (!els[id]) els[id] = new El("div");
        return els[id];
    },
    querySelectorAll() {
        return [];
    },
    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    },
};

for (const id of htmlIds) els[id] = new El("div");

// --- red: doble estricto ---------------------------------------------------
const requests = [];
let streamMode = "hold"; // "hold" retiene el stream; "close" lo cierra al instante
let releaseStream = null;
let projectsMode = "empty"; // "empty" no hay sesiones; "one" hay una sesión

function jsonResponse(body) {
    return {
        ok: true,
        status: 200,
        statusText: "OK",
        json: async () => body,
        text: async () => JSON.stringify(body),
    };
}

function sseResponse() {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    releaseStream = release;
    let sent = false;
    return {
        ok: true,
        status: 200,
        statusText: "OK",
        body: {
            getReader() {
                return {
                    async read() {
                        if (!sent) {
                            sent = true;
                            const payload = { reply: "listo", stop_reason: "stop", model: "stub" };
                            const frame = "event: done\ndata: " + JSON.stringify(payload) + "\n\n";
                            return { done: false, value: new TextEncoder().encode(frame) };
                        }
                        if (streamMode === "close") return { done: true, value: undefined };
                        await gate;
                        return { done: true, value: undefined };
                    },
                };
            },
        },
    };
}

function route(url, options) {
    const method = (options && options.method) || "GET";
    const path = String(url);
    if (method === "GET" && path === "/studio/api/health") {
        return jsonResponse({ server: "ok", comfyui: "ok", llm_model: "stub" });
    }
    if (method === "GET" && path === "/studio/api/status") {
        return jsonResponse({ llm: { model: "stub" } });
    }
    if (method === "GET" && path === "/studio/api/projects") {
        // projectsMode permite arrancar sin ninguna sesión creada
        if (projectsMode === "empty") return jsonResponse({ projects: [] });
        const one = { id: "proj-1", name: "Sesión 1", aspect_ratio: "16:9" };
        return jsonResponse({ projects: [one] });
    }
    if (method === "GET" && /^\/studio\/api\/projects\/[^/]+\/chat\?limit=\d+$/.test(path)) {
        return jsonResponse({ messages: [] });
    }
    if (method === "GET" && /^\/studio\/api\/productions\?project_id=/.test(path)) {
        return jsonResponse({ productions: [] });
    }
    if (method === "POST" && /^\/studio\/api\/projects\/[^/]+\/chat$/.test(path)) {
        return sseResponse();
    }
    throw new Error("petici\u00F3n inesperada: " + method + " " + path);
}

const fetchDouble = (url, options) => {
    const method = (options && options.method) || "GET";
    requests.push({ url: String(url), method, body: options && options.body });
    try {
        return Promise.resolve(route(url, options));
    } catch (err) {
        return Promise.reject(err);
    }
};

class EventSourceDouble {
    constructor(url) {
        this.url = url;
        this.listeners = {};
    }
    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    close() {}
}

const sandbox = {
    document: doc,
    window: {
        // temporizadores reales: el aviso se oculta solo a los 6000 ms
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (t) => clearTimeout(t),
        setInterval: () => 0,
        clearInterval: () => {},
    },
    fetch: fetchDouble,
    EventSource: EventSourceDouble,
    TextDecoder,
    TextEncoder,
    console,
    JSON,
    Promise,
    setTimeout,
    clearTimeout,
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(agentJs, sandbox, { filename: "panel/agent.js" });

// único punto de entrada extra: recargar sesiones con el botón real del panel
sandbox.__loadProjects = () => doc.querySelector("#agent-refresh-projects").dispatch("click");

const flush = async (rounds) => {
    for (let i = 0; i < (rounds || 12); i += 1) await new Promise((r) => setTimeout(r, 0));
};

const chatPosts = () => requests.filter((r) => r.method === "POST" && /\/chat$/.test(r.url));

const composer = () => doc.querySelector("#agent-composer-form");
const input = () => doc.querySelector("#agent-composer-input");
const sendBtn = () => doc.querySelector("#agent-send-btn");
const toastEl = () => doc.querySelector("#agent-toast");

function submit(text) {
    input().value = text;
    composer().requestSubmit();
}

const results = {};

async function run() {
    // boot() sin sesiones: loadProjects no selecciona nada
    for (const fn of doc.listeners.DOMContentLoaded || []) fn({ type: "DOMContentLoaded" });
    await flush(20);

    // 1) sin sesi\u00F3n: el borrador debe sobrevivir y no debe haber POST de chat
    const before = chatPosts().length;
    submit("borrador sin sesi\u00F3n");
    await flush(10);
    results.noSession = {
        value: input().value,
        posts: chatPosts().length - before,
        toast: toastEl().textContent,
        toastHidden: toastEl().hidden,
        btnDisabled: sendBtn().disabled,
    };

    // 2) env\u00EDo permitido: un \u00FAnico POST a la ruta real, compositor vac\u00EDo
    projectsMode = "one";
    sandbox.__loadProjects();
    await flush(20);
    const beforeOk = chatPosts().length;
    submit("primer turno");
    await flush(10);
    results.allowed = {
        value: input().value,
        posts: chatPosts().length - beforeOk,
        urls: chatPosts().slice(beforeOk).map((r) => r.url),
        bodies: chatPosts().slice(beforeOk).map((r) => r.body),
        btnDisabled: sendBtn().disabled,
    };

    // 3) turno pendiente (stream retenido): el nuevo borrador se conserva
    const beforeBusy = chatPosts().length;
    submit("segundo intento");
    await flush(10);
    results.busy = {
        value: input().value,
        posts: chatPosts().length - beforeBusy,
        toast: toastEl().textContent,
        toastHidden: toastEl().hidden,
        btnDisabled: sendBtn().disabled,
    };

    // 4) texto vac\u00EDo: no se env\u00EDa nada
    const beforeEmpty = chatPosts().length;
    submit("   ");
    await flush(10);
    results.empty = { value: input().value, posts: chatPosts().length - beforeEmpty };

    // 5) al terminar el turno se recuperan bot\u00F3n y foco
    streamMode = "close";
    if (typeof releaseStream === "function") releaseStream();
    await flush(30);
    results.afterTurn = {
        btnDisabled: sendBtn().disabled,
        focused: doc.activeElement === input(),
    };

    // cualquier ruta fuera de las declaradas en route() ya habría rechazado el fetch
    results.unexpected = requests.filter((r) => !/^\/studio\/api\//.test(r.url)).map((r) => r.url);
    results.requestCount = requests.length;
    process.stdout.write(JSON.stringify(results));
}

run().catch((err) => {
    process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }));
    process.exitCode = 1;
});
"""


def _run_harness() -> dict:
    proc = subprocess.run(
        [NODE, "-e", HARNESS],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env={**os.environ, "AGENT_JS": str(AGENT_JS), "AGENT_HTML": str(AGENT_HTML)},
    )
    assert proc.returncode == 0, f"harness fall\u00F3:\n{proc.stdout}\n{proc.stderr}"
    payload = json.loads(proc.stdout)
    assert "error" not in payload, payload["error"]
    return payload


@pytest.fixture(scope="module")
def run() -> dict:
    return _run_harness()


def test_sin_sesion_conserva_borrador_y_no_envia(run: dict) -> None:
    """Sin sesi\u00F3n seleccionada el texto no se pierde y no hay POST de chat."""
    case = run["noSession"]
    assert case["posts"] == 0, "no debe emitirse un POST de chat sin sesi\u00F3n"
    assert case["value"] == "borrador sin sesi\u00F3n", "el borrador debe conservarse"
    assert case["toastHidden"] is False, "debe mostrarse un aviso al usuario"
    assert "sesi\u00F3n" in case["toast"].lower()
    assert case["btnDisabled"] is False, "sin turno en curso el bot\u00F3n sigue disponible"


def test_turno_pendiente_conserva_borrador_y_no_duplica(run: dict) -> None:
    """Con un turno en curso el segundo intento conserva el texto y no reenv\u00EDa."""
    case = run["busy"]
    assert case["posts"] == 0, "no debe emitirse un segundo POST mientras hay turno pendiente"
    assert case["value"] == "segundo intento", "el nuevo borrador debe conservarse"
    assert case["toastHidden"] is False, "debe mostrarse un aviso al usuario"
    assert "turno" in case["toast"].lower()
    assert case["btnDisabled"] is True, "el bot\u00F3n sigue bloqueado durante el turno"


def test_envio_permitido_vacia_y_emite_un_post(run: dict) -> None:
    """Un env\u00EDo v\u00E1lido vac\u00EDa el compositor y hace un \u00FAnico POST real."""
    case = run["allowed"]
    assert case["posts"] == 1, "debe emitirse exactamente un POST de chat"
    assert case["urls"] == ["/studio/api/projects/proj-1/chat"]
    assert json.loads(case["bodies"][0]) == {"message": "primer turno", "stream": True}
    assert case["value"] == "", "el compositor debe vaciarse tras un env\u00EDo v\u00E1lido"


def test_texto_vacio_no_envia(run: dict) -> None:
    """Un compositor vac\u00EDo o solo con espacios no genera petici\u00F3n."""
    case = run["empty"]
    assert case["posts"] == 0
    assert case["value"] == "   "


def test_al_terminar_el_turno_recupera_boton_y_foco(run: dict) -> None:
    """Al cerrarse el stream se rehabilita el bot\u00F3n y vuelve el foco al compositor."""
    case = run["afterTurn"]
    assert case["btnDisabled"] is False
    assert case["focused"] is True


def test_no_hay_peticiones_inesperadas(run: dict) -> None:
    """El doble de red rechaza cualquier ruta fuera de /studio/api/."""
    assert run["unexpected"] == []
    assert run["requestCount"] > 0, "el doble de red debe haber registrado peticiones"
