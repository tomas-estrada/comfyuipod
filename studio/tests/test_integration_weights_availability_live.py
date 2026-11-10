"""Integración: «200 en health» NO prueba que los pesos del modelo existan.

Riesgo pagado en dinero: un pod donde ``download-models.sh`` no entregó los
archivos sigue respondiendo 200 en el healthz, el panel ofrece plantillas que
luego revientan a mitad de render, y el gasto de GPU ya está contratado. Este
test fija el contrato real, levantando el studio-server como proceso hijo
sobre TCP loopback (sin ASGI in-process) contra un manifiesto de modelos
artificial en un tmp:

  * el health 200 NO afirma nada sobre los pesos (no lo comprueba);
  * el catálogo SÍ lo hace: ``available`` es un hecho del filesystem, recalculado
    en cada request — sin pesos en disco, las plantillas opt-in se marcan NO
    disponibles con su razón;
  * aparecer los archivos en disco (sin reiniciar el servidor) los vuelve
    disponibles al siguiente GET: la fuente de verdad es ``stat()``, no el env;
  * un archivo de cero bytes cuenta como NO presente (``st_size > 0``);
  * ``/studio/api/status`` → ``model_assets.opt_in_groups`` coincide con el disco.

Sin GPU, sin internet, sin claves. El manifiesto y el COMFY_DATA_DIR apuntan a
tmp dirs propios de la prueba (env del proceso hijo en el arranque: cero
monkeypatch de código de producción).
"""

from __future__ import annotations

import json
import os
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
_POLL_ATTEMPT_TIMEOUT_S = 8.0
_POLL_STEP_S = 0.2

_THREE_D_DEST = "models/checkpoints/hunyuan_fake_v1.safetensors"
_ANIMATE_DEST = "models/diffusion_models/wan_animate_fake.safetensors"


def _studio_root() -> Path:
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


def _spawn_server(port: int, extra_env: dict[str, str], log_path: Path) -> subprocess.Popen:
    root = _studio_root()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(root), env.get("PYTHONPATH", "")]))
    env.update(extra_env)
    with log_path.open("wb") as log_handle:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "comfy_studio.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(root),
        )


def _terminate(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
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
    proc: subprocess.Popen,
    diagnostics: Callable[[], str] = lambda: "",
) -> None:
    deadline = time.monotonic() + deadline_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"{label}: el proceso hijo murió (exit={proc.returncode}). {diagnostics()}")
        try:
            if check():
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(_POLL_STEP_S)
    detail = f"último error: {last_error!r}" if last_error else "invariante nunca visto"
    pytest.fail(f"{label} no convergió en {deadline_s:.0f}s — {detail}. {diagnostics()}")


def _fake_manifest() -> dict:
    """Manifiesto mínimo con dos grupos opt-in (los ids de destino son los reales)."""
    return {
        "version": 1,
        "meta": {"note": "manifiesto artificial de prueba"},
        "models": [
            {
                "class": "optional",
                "opt_in": "STUDIO_INCLUDE_3D",
                "dest_path": _THREE_D_DEST,
                "bytes": 8,
            },
            {
                "class": "optional",
                "opt_in": "STUDIO_INCLUDE_ANIMATE",
                "dest_path": _ANIMATE_DEST,
                "bytes": 8,
            },
        ],
    }


@pytest.fixture(scope="module")
def live_weights_stack(tmp_path_factory: pytest.TempPathFactory):
    """studio-server real apuntando a un manifiesto tmp con pesos opt-in «pedidos»."""
    assert _studio_root() == Path(__file__).resolve().parents[1], (
        "el proceso de prueba importa comfy_studio de otro árbol; "
        "corre pytest desde la raíz del checkout para que studio/ mande"
    )
    workdir = tmp_path_factory.mktemp("live-weights")
    data_dir = workdir / "studio_data"
    comfy_root = workdir / "comfy-data"
    manifest_path = workdir / "models-studio-test.json"
    manifest_path.write_text(json.dumps(_fake_manifest()), encoding="utf-8")
    server_port = _free_port()
    # Puerto libre que nunca se enlaza: el upstream es determinísticamente
    # inalcanzable (no depende de si el dev tiene un ComfyUI en 8188).
    dead_comfyui_port = _free_port()
    server_log = workdir / "studio-server.log"

    server = _spawn_server(
        server_port,
        {
            "STUDIO_DATA_DIR": str(data_dir),
            "STUDIO_SERVER_WORKERS": "1",
            "STUDIO_STUB_LLM": "1",
            "STUDIO_RECONCILE_INTERVAL_S": "0",
            "STUDIO_RESUME_JOBS": "0",
            "STUDIO_PERCEPTION_BACKEND": "sim",
            "STUDIO_ML_TIMEOUT_S": "5",
            "MODEL_PROFILE": "studio",
            # Simulación explícita (desde #215, perfil studio exigiría motores
            # reales y el server moriría sin GPU): misma postura que CI.
            "STUDIO_ORCH_REAL_ENGINES": "0",
            "STUDIO_LLM_KEY": "",
            "STUDIO_LLM_FALLBACK_KEY": "",
            "STUDIO_COMFYUI_URL": f"http://127.0.0.1:{dead_comfyui_port}",
            # Pesos «pedidos» (intención) + dónde viven (filesystem real bajo tmp):
            # la disponibilidad debe juzgarse contra el disco, no contra el env.
            "STUDIO_INCLUDE_3D": "1",
            "STUDIO_INCLUDE_ANIMATE": "1",
            "STUDIO_MANIFEST": str(manifest_path),
            "COMFY_DATA_DIR": str(comfy_root),
        },
        server_log,
    )
    try:
        base_url = f"http://127.0.0.1:{server_port}"

        def _ready() -> bool:
            resp = httpx.get(f"{base_url}/studio/api/health", timeout=_POLL_ATTEMPT_TIMEOUT_S)
            return resp.status_code == 200

        _wait_until(
            "studio-server listo",
            _ready,
            _BOOT_DEADLINE_S,
            proc=server,
            diagnostics=lambda: f"log={_log_tail(server_log)!r}",
        )
        yield SimpleNamespace(
            server=server,
            base_url=base_url,
            comfy_root=comfy_root,
        )
    finally:
        _terminate(server)


def _templates_by_id(stack: SimpleNamespace) -> dict[str, dict]:
    resp = httpx.get(f"{stack.base_url}/studio/api/templates", timeout=_REQUEST_TIMEOUT_S)
    assert resp.status_code == 200
    return {t["id"]: t for t in resp.json()["templates"]}


def _weight_file(stack: SimpleNamespace, dest: str) -> Path:
    # Misma regla que resolve_dest(): "models/..." vive bajo COMFY_DATA_DIR/models.
    return stack.comfy_root.joinpath(*Path(dest).parts)


def test_health_200_no_afirma_que_los_pesos_existan(live_weights_stack: SimpleNamespace) -> None:
    """El contrato honesto: health 200 con pesos AUSENTES (y upstream caído).

    Si alguien «arregla» el health para afirmar modelos sin verificarlos, o le
    añade un assert que devuelva 502 cuando faltan pesos, esta prueba lo frena:
    el health 200 no dice nada de los pesos; quien los juzga es el catálogo.
    """
    resp = httpx.get(f"{live_weights_stack.base_url}/studio/api/health", timeout=_REQUEST_TIMEOUT_S)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    # Sin doble de ComfyUI en este flujo: degrada visible, pero sigue siendo 200.
    assert payload["comfyui"] == "degraded"
    assert payload["edge_auth"] == "open"


def test_catalogo_declara_indisponibles_sin_pesos_en_disco(
    live_weights_stack: SimpleNamespace,
) -> None:
    """Grupos opt-in pedidos (env) pero sin archivos: el catálogo los marca NO disponibles."""
    by_id = _templates_by_id(live_weights_stack)
    three_d = by_id["hunyuan3d_image_to_3d"]
    animate = by_id["wan_animate"]
    assert three_d["available"] is False
    assert three_d["unavailable_reason"]
    assert animate["available"] is False
    assert animate["unavailable_reason"]
    # minimax no se pidió ni está: también indisponible (no es un agujo del gate).
    assert by_id["minimax_r2v"]["available"] is False
    # Y las que no necesitan pesos opt-in sí están disponibles.
    assert by_id["wan_t2v"]["available"] is True


def test_archivo_de_cero_bytes_no_es_un_peso(live_weights_stack: SimpleNamespace) -> None:
    """Descarga truncada (archivo existe, tamaño 0) NO vuelve disponible la plantilla."""
    target = _weight_file(live_weights_stack, _THREE_D_DEST)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()  # 0 bytes: existe pero no es un peso

    by_id = _templates_by_id(live_weights_stack)
    assert by_id["hunyuan3d_image_to_3d"]["available"] is False


def test_pesos_reales_aparecen_sin_reiniciar_y_el_catalogo_vira(
    live_weights_stack: SimpleNamespace,
) -> None:
    """El filesystem manda: crear los archivos (sin restart) vuelve disponibles las plantillas.

    Sin el servidor en marcha no hay forma de que un estado cacheado de arranque
    explique el cambio: ``available`` se recalcula request a request contra
    ``stat()``. Y ``/studio/api/status`` rinde cuentas por grupo (present/missing).
    """
    for dest in (_THREE_D_DEST, _ANIMATE_DEST):
        target = _weight_file(live_weights_stack, dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"peso-fake-0123")

    by_id = _templates_by_id(live_weights_stack)
    assert by_id["hunyuan3d_image_to_3d"]["available"] is True
    assert by_id["hunyuan3d_image_to_3d"]["unavailable_reason"] is None
    assert by_id["wan_animate"]["available"] is True
    # minimax sigue sin pesos en el manifiesto de prueba: sigue indisponible.
    assert by_id["minimax_r2v"]["available"] is False

    status = httpx.get(
        f"{live_weights_stack.base_url}/studio/api/status", timeout=_REQUEST_TIMEOUT_S
    )
    assert status.status_code == 200
    groups = status.json()["model_assets"]["opt_in_groups"]
    assert groups["three_d"]["present"] is True
    assert groups["three_d"]["missing"] == []
    assert groups["animate"]["present"] is True
    assert groups["minimax"]["present"] is False
