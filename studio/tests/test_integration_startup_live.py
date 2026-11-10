"""Integración: arranque real del studio-server sobre TCP loopback (sin GPU).

Esta prueba levanta DOS procesos reales (no ASGI in-process):

  1. ``comfy_studio.stubs.comfyui`` — doble local de prueba, claramente nombrado
     como tal, que solo emite el comportamiento a verificar: ComfyUI vivo
     respondiendo ``/system_stats`` por HTTP real sobre loopback.
  2. ``python -m comfy_studio.server`` — el ENTRYPOINT REAL que el supervisor
     (comfyctl) lanza en el pod, aquí en proceso hijo sobre un puerto libre.

Invariantes del flujo de arranque que esta prueba protege (historial real: el
supervisor corría el strict-check antes de que el studio-server estuviera listo
y nginx devolvía 502):

  * el Basic Auth se exige desde el arranque (un pod mal configurado no abre
    el borde en silencio);
  * ``/studio/api/health`` converge a 200 dentro de un deadline acotado y su
    sonda al upstream viaja por socket real (``comfyui: ok`` con el stub TCP);
  * la capa de orquestación reporta ``ready`` desde el arranque (S15 lee ese
    campo; un grafo no construible debe verse aquí, no a mitad de un reel);
  * si ComfyUI muere, health sigue respondiendo 200 pero declara
    ``comfyui: "degraded"``: un 200 del healthz NO prueba que el flujo esté
    vivo, y el studio-server no debe romperse al morir su upstream.

Sin GPU, sin internet, sin secretos: las credenciales son aleatorias por
ejecución y el data-dir es un tmp. Los procesos hijos se limpian siempre
(terminate + wait acotado, kill como último recurso), no hay sleeps fijos
largos ni puertos fijos. Corre en Linux y Windows (solo usa subprocess y
``terminate()``, ambos portables).
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import comfy_studio

_REQUEST_TIMEOUT_S = 10.0
_BOOT_DEADLINE_S = 45.0
# Sondeo de readiness: presupuesto por intento generoso (bajo carga de CI/local
# un health puede tardar segundos porque él mismo sondea el upstream), con
# deadline total acotado y muerte inmediata si el hijo ya no vive.
_POLL_ATTEMPT_TIMEOUT_S = 8.0
_DEGRADE_DEADLINE_S = 30.0
_POLL_STEP_S = 0.2


def _studio_root() -> Path:
    """Directorio ``studio/`` del árbol que este proceso de prueba está usando."""
    return Path(comfy_studio.__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return "<sin log>"


def _spawn(module: str, port: int, extra_env: dict[str, str], log_path: Path) -> subprocess.Popen:
    """Lanza ``python -m <module> --host 127.0.0.1 --port <port>`` como hijo real.

    PYTHONPATH se fuerza al ``studio/`` de ESTE checkout para que el hijo ejecute
    el mismo código que importa la prueba, no un editable-install de otro clone.
    stdout/stderr van a un archivo tmp (nunca a una pipe sin lector, que llenaría
    el buffer y congelaría al servidor).
    """
    root = _studio_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH", "")]))
    env.update(extra_env)
    with log_path.open("wb") as log_handle:
        return subprocess.Popen(
            [sys.executable, "-m", module, "--host", "127.0.0.1", "--port", str(port)],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(root),
        )


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _wait_until(
    label: str,
    check: Callable[[], bool],
    deadline_s: float,
    diagnostics: Callable[[], str] = lambda: "",
    proc: subprocess.Popen | None = None,
) -> None:
    """Poll hasta que ``check()`` sea verdad; falla en vez de colgarse.

    Sin sleeps fijos largos: paso corto y deadline duro. Las excepciones httpx
    durante el arranque son esperables (connection refused mientras el hijo
    hace el bind) y se reintentan dentro del presupuesto; si el hijo murió, se
    aborta de inmediato con su log en el mensaje.
    """
    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            pytest.fail(f"{label}: el proceso hijo murió (exit={proc.returncode}). {diagnostics()}")
        try:
            if check():
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(_POLL_STEP_S)
    detail = f"último error: {last_error!r}" if last_error else "invariante nunca visto"
    pytest.fail(f"{label} no convergió en {deadline_s:.0f}s — {detail}. {diagnostics()}")


@pytest.fixture(scope="module")
def live_stack(tmp_path_factory: pytest.TempPathFactory):
    """studio-server real + doble TCP de ComfyUI, ambos en loopback con puertos libres."""
    assert _studio_root() == Path(__file__).resolve().parents[1], (
        "el proceso de prueba importa comfy_studio de otro árbol; "
        "corre pytest desde la raíz del checkout para que studio/ mande"
    )
    workdir = tmp_path_factory.mktemp("live-boot")
    data_dir = workdir / "studio_data"
    stub_port = _free_port()
    server_port = _free_port()
    user = "itest"
    password = secrets.token_hex(16)  # credencial efímera de prueba, no un secreto
    stub_log = workdir / "stub-comfyui.log"
    server_log = workdir / "studio-server.log"

    stub = _spawn("comfy_studio.stubs.comfyui", stub_port, {}, stub_log)
    server: subprocess.Popen | None = None
    try:
        base_url = f"http://127.0.0.1:{server_port}"

        def _stub_up() -> bool:
            resp = httpx.get(
                f"http://127.0.0.1:{stub_port}/system_stats",
                timeout=_POLL_ATTEMPT_TIMEOUT_S,
            )
            return resp.status_code == 200

        _wait_until(
            "el doble TCP de ComfyUI en listening",
            _stub_up,
            _BOOT_DEADLINE_S,
            diagnostics=lambda: f"log={_log_tail(stub_log)!r}",
            proc=stub,
        )

        server = _spawn(
            "comfy_studio.server",
            server_port,
            {
                "STUDIO_DATA_DIR": str(data_dir),
                "STUDIO_COMFYUI_URL": f"http://127.0.0.1:{stub_port}",
                "STUDIO_STUB_LLM": "1",
                "STUDIO_RECONCILE_INTERVAL_S": "0",
                "STUDIO_RESUME_JOBS": "0",
                "STUDIO_SERVER_WORKERS": "1",
                "MODEL_PROFILE": "studio",
                # Desde #215, perfil studio exige motores reales por defecto y
                # el server FALLA al arrancar sin GPU verificada. La simulación
                # es elección explícita (igual que hace CI en el paso de smoke).
                "STUDIO_ORCH_REAL_ENGINES": "0",
                # Modo soportado por producto para entornos sin GPU/pesos: evita
                # que el sondeo de percepción dispare workers ML de subprocess en
                # cada arranque (la sonda puede tardar minutos en máquinas sin
                # torch). No toca lo que se prueba: arranque, auth y health.
                "STUDIO_PERCEPTION_BACKEND": "sim",
                "STUDIO_ML_TIMEOUT_S": "5",
                # Postura de claves fija: una máquina de dev con STUDIO_LLM_KEY
                # propia no puede cambiar lo que el pod de prueba afirma.
                "STUDIO_LLM_KEY": "",
                "STUDIO_LLM_FALLBACK_KEY": "",
                "COMFY_USER": user,
                "COMFY_PASS": password,
            },
            server_log,
        )

        def _server_ready() -> bool:
            resp = httpx.get(
                f"{base_url}/studio/api/health",
                auth=(user, password),
                timeout=_POLL_ATTEMPT_TIMEOUT_S,
            )
            return resp.status_code == 200

        # El readiness gate que comfyctl espera ANTES del strict-check (lección
        # del 502): el health 200 debe converger, no asumirse.
        _wait_until(
            "studio-server listo (GET /studio/api/health == 200)",
            _server_ready,
            _BOOT_DEADLINE_S,
            diagnostics=lambda: f"log={_log_tail(server_log)!r}",
            proc=server,
        )
        yield SimpleNamespace(
            base_url=base_url,
            auth=(user, password),
            stub=stub,
            server=server,
            server_log=server_log,
        )
    finally:
        for proc in (server, stub):
            if proc is not None:
                _terminate(proc)


def test_arranque_exige_basic_auth_en_el_borde(live_stack: SimpleNamespace) -> None:
    """Sin credenciales (y con credenciales malas) el borde responde 401 desde el arranque."""
    health_url = f"{live_stack.base_url}/studio/api/health"
    anon = httpx.get(health_url, timeout=_REQUEST_TIMEOUT_S)
    assert anon.status_code == 401
    assert "www-authenticate" in {k.lower() for k in anon.headers}

    wrong = httpx.get(
        health_url,
        auth=(live_stack.auth[0], "clave-distinta-" + secrets.token_hex(8)),
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert wrong.status_code == 401


def test_health_converge_y_ve_el_upstream_por_socket_real(live_stack: SimpleNamespace) -> None:
    """Tras el readiness gate, health 200 con la sonda TCP al doble de ComfyUI visible.

    Sondea hasta que el payload esté completo dentro del deadline (bajo arranque
    en frío la sonda interna de 2 s puede expirar una vez); el assert final es
    estricto: si el flujo no converge a healthy, la prueba falla.
    """
    payload: dict = {}

    def _healthy() -> bool:
        nonlocal payload
        resp = httpx.get(
            f"{live_stack.base_url}/studio/api/health",
            auth=live_stack.auth,
            timeout=_REQUEST_TIMEOUT_S,
        )
        if resp.status_code != 200:
            return False
        payload = resp.json()
        return payload.get("comfyui") == "ok"

    _wait_until(
        "health 200 con comfyui: ok vía socket real",
        _healthy,
        _DEGRADE_DEADLINE_S,
        diagnostics=lambda: f"log={_log_tail(live_stack.server_log)!r}",
        proc=live_stack.server,
    )
    assert payload["status"] == "ok"
    assert payload["server"] == "ok"
    # La sonda viaja por HTTP real contra el doble en loopback: esto es un socket
    # de verdad, no un transporte ASGI inyectado.
    assert payload["comfyui"] == "ok"
    assert payload["comfyui_reachable"] is True
    assert payload["comfyui_devices"] >= 1
    assert payload["edge_auth"] == "basic"
    assert payload["stub_llm"] is True
    assert payload["model_profile"] == "studio"
    assert payload["llm_key_status"] == "<no definida>"
    # S15 del strict-check lee este campo; un grafo no construible debe verse aquí.
    assert payload["orchestration"]["state"] == "ready"
    assert payload["orchestration"]["graph_buildable"] is True


def test_catalogo_de_plantillas_sirve_tras_el_arranque(live_stack: SimpleNamespace) -> None:
    """El catálogo de plantillas responde por HTTP real y cada entrada declara su disponibilidad."""
    resp = httpx.get(
        f"{live_stack.base_url}/studio/api/templates",
        auth=live_stack.auth,
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert resp.status_code == 200
    templates = resp.json()["templates"]
    assert len(templates) == 17
    by_id = {t["id"]: t for t in templates}
    assert "wan_t2v" in by_id
    assert all(isinstance(t["available"], bool) for t in templates)
    # Sin los grupos opt-in en disco, las plantillas que los exigen se marcan
    # NO disponibles (política: «disponible» es un hecho del filesystem).
    assert by_id["hunyuan3d_image_to_3d"]["available"] is False
    assert by_id["wan_animate"]["available"] is False
    assert by_id["minimax_r2v"]["available"] is False


def test_muere_el_upstream_y_health_sigue_200_pero_degraded(
    live_stack: SimpleNamespace,
) -> None:
    """ComfyUI (el doble TCP) muere: el pod no se cae ni miente.

    Invariante pagado en dinero real: un 200 de health NO prueba que el flujo
    esté vivo. Al morir el upstream, health debe seguir 200 (el studio vive)
    pero declarar ``comfyui: degraded``, y el catálogo debe seguir sirviendo.
    """
    _terminate(live_stack.stub)

    def _degraded() -> bool:
        resp = httpx.get(
            f"{live_stack.base_url}/studio/api/health",
            auth=live_stack.auth,
            timeout=_REQUEST_TIMEOUT_S,
        )
        assert resp.status_code == 200, "health debe seguir 200 aunque el upstream muera"
        return resp.json()["comfyui"] == "degraded"

    _wait_until(
        "health declara comfyui: degraded tras matar el upstream",
        _degraded,
        _DEGRADE_DEADLINE_S,
        diagnostics=lambda: f"server poll={live_stack.server.poll()}",
    )
    resp = httpx.get(
        f"{live_stack.base_url}/studio/api/health",
        auth=live_stack.auth,
        timeout=_REQUEST_TIMEOUT_S,
    )
    payload = resp.json()
    assert payload["comfyui"] == "degraded"
    assert payload["comfyui_reachable"] is False
    assert payload["server"] == "ok"

    catalog = httpx.get(
        f"{live_stack.base_url}/studio/api/templates",
        auth=live_stack.auth,
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert catalog.status_code == 200
