"""Integración: aprobar la misma producción dos veces a la vez por HTTP real.

Riesgo pagado en dinero: la aprobación humana dispara el lazo CARO (ejecución +
ensamblaje). Si dos peticaciones concurrentes (dobles clicks, reintentos del
panel tras un timeout) corren el grafo dos veces, el pod gasta GPU dos veces.
La unidad ya fuerza la carrera con un grafo bloqueante inyectado; este test no
parchea nada y ejercita la superficie donde el fallo aparecería en producción:

  * el proceso REAL del studio-server (uvicorn + threadpool de Starlette),
    no TestClient in-process: es el threadpool real el que superpone los
    `approve_plan`;
  * el checkpointer SQLite REAL en modo WAL con sus locks de escritura
    (el compare-and-set de `claim_phase` vive ahí);
  * dos POST simultáneos (barrera del lado cliente) contra la misma producción.

Invariantes verificadas (sin importar la interleaving concreta):
  1. ambas respuestas son 2xx y coherentes — nunca 409/500 ni estado corrupto;
  2. el checkpoint persistido registra EXACTAMENTE un evento `approved`
     (la producción fue reclamada una sola vez);
  3. el estado final es terminal y completo, sin artefacto falso en simulación;
  4. aprobar de nuevo tras el terminal es replay puro: no añade ni un evento.

No depende del contenido del plan: el planner disponible en CI es determinista,
pero todas las aserciones se derivan del propio plan devuelto por la API
(número de shots, ids), no de supuestos sobre él. Sin GPU, sin internet, sin
claves: el lazo corre en modo simulado (colaboradores stub de producto) y todo
el estado vive en un tmp dir.
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import comfy_studio
from comfy_studio.orchestration import ProductionCheckpointer

_REQUEST_TIMEOUT_S = 30.0
_BOOT_DEADLINE_S = 45.0
_POLL_ATTEMPT_TIMEOUT_S = 8.0
_POLL_STEP_S = 0.2


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


@pytest.fixture(scope="module")
def live_approve_stack(tmp_path_factory: pytest.TempPathFactory):
    """studio-server real con checkpointer SQLite en un tmp propio del test."""
    assert _studio_root() == Path(__file__).resolve().parents[1], (
        "el proceso de prueba importa comfy_studio de otro árbol; "
        "corre pytest desde la raíz del checkout para que studio/ mande"
    )
    workdir = tmp_path_factory.mktemp("live-approve")
    data_dir = workdir / "studio_data"
    orch_dir = workdir / "orchestration"
    server_port = _free_port()
    dead_comfyui_port = _free_port()
    server_log = workdir / "studio-server.log"

    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(_studio_root()), env.get("PYTHONPATH", "")])
    )
    env.update(
        {
            "STUDIO_DATA_DIR": str(data_dir),
            # El checkpoint lo abre el PROCESO SERVIDOR bajo esta ruta; el test
            # lo reabre después por el mismo path: dos conexiones SQLite reales
            # (WAL) a la misma BD, como en el pod.
            "STUDIO_ORCH_CHECKPOINT_DIR": str(orch_dir),
            "STUDIO_COMFYUI_URL": f"http://127.0.0.1:{dead_comfyui_port}",
            "STUDIO_SERVER_WORKERS": "1",
            "STUDIO_STUB_LLM": "1",
            "STUDIO_RECONCILE_INTERVAL_S": "0",
            "STUDIO_RESUME_JOBS": "0",
            "STUDIO_PERCEPTION_BACKEND": "sim",
            "STUDIO_ML_TIMEOUT_S": "5",
            "MODEL_PROFILE": "studio",
            # Simulación explícita del lazo de producción (desde #215 el perfil
            # studio exigiría motores reales): misma postura que CI en el smoke.
            "STUDIO_ORCH_REAL_ENGINES": "0",
            "STUDIO_LLM_KEY": "",
            "STUDIO_LLM_FALLBACK_KEY": "",
        }
    )
    with server_log.open("wb") as log_handle:
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "comfy_studio.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(server_port),
            ],
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(_studio_root()),
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
            base_url=base_url,
            server=server,
            server_log=server_log,
            checkpoint_db=orch_dir / "checkpoints.sqlite3",
        )
    finally:
        _terminate(server)


# ---------------------------------------------------------------------------
# clientes sobre el servidor vivo (sin parches de ningún tipo)
# ---------------------------------------------------------------------------


def _create_production(stack: SimpleNamespace, idea: str) -> dict:
    resp = httpx.post(
        f"{stack.base_url}/studio/api/productions",
        json={"idea": idea, "cost_cap_usd": 1.0},
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _get_plan(stack: SimpleNamespace, production_id: str) -> dict:
    resp = httpx.get(
        f"{stack.base_url}/studio/api/productions/{production_id}",
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["plan"]


def _approve(stack: SimpleNamespace, production_id: str) -> httpx.Response:
    return httpx.post(
        f"{stack.base_url}/studio/api/productions/{production_id}/approve",
        timeout=_REQUEST_TIMEOUT_S,
    )


def _count_log(log: list[dict], kind: str) -> int:
    return sum(1 for entry in log if entry.get("kind") == kind)


def test_plan_por_http_deja_la_puerta_en_awaiting_approval(live_approve_stack) -> None:
    """El borrador barato NO gasta el lazo: 201 con phase awaiting_approval y plan visible."""
    out = _create_production(live_approve_stack, "puerta de gasto sobre HTTP real")
    assert out["phase"] == "awaiting_approval"
    plan = _get_plan(live_approve_stack, out["production_id"])
    assert plan["phase"] == "awaiting_approval"
    assert plan["storyboard_approved"] is False
    # Toda producción planificada tiene al menos un shot con capas: si el plan
    # llegara vacío por HTTP, aprobar no tendría nada que ejecutar.
    assert len(plan["sequences"]) >= 1
    assert all(seq["shots"] for seq in plan["sequences"])


def test_dos_aprobaciones_simultaneas_reclaman_el_lazo_una_sola_vez(
    live_approve_stack,
) -> None:
    """Dos POST /approve concurrentes: ambas 2xx y UN solo reclamo del grafo persistido."""
    production_id = _create_production(live_approve_stack, "doble clic en aprobar")["production_id"]

    barrier = threading.Barrier(2)

    def _fire() -> httpx.Response:
        barrier.wait(timeout=20)
        return _approve(live_approve_stack, production_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_fire), pool.submit(_fire)]
        responses = [f.result(timeout=60) for f in futures]

    # 1) Ninguna llamada muere: 409/500 aquí significaría carrera mal resuelta.
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    bodies = [r.json() for r in responses]

    # 2) Formas coherentes: quien compite ve la ejecución en curso (executing +
    #    already_in_progress); quien llega tras el terminal recibe replay terminal.
    in_progress = [b for b in bodies if b.get("already_in_progress")]
    for body in in_progress:
        assert body["phase"] == "executing"
    assert len(in_progress) <= 1
    finished = [b for b in bodies if not b.get("already_in_progress")]
    assert len(finished) >= 1
    for body in finished:
        assert body["phase"] in ("simulated", "done"), body

    # 3) El checkpoint persistido: UN solo `approved` (el grafo fue reclamado
    #    una vez) y como mucho un `execute` por shot (ningún shot corrió dos veces).
    plan = _get_plan(live_approve_stack, production_id)
    assert _count_log(plan["log"], "approved") == 1
    shot_ids = [shot["id"] for seq in plan["sequences"] for shot in seq["shots"]]
    executed = [entry["shot"] for entry in plan["log"] if entry.get("kind") == "execute"]
    assert len(executed) == len(set(executed))
    assert set(executed) <= set(shot_ids)

    # 4) Estado final terminal y honesto (simulación no afirma artefacto).
    status = httpx.get(
        f"{live_approve_stack.base_url}/studio/api/productions/{production_id}/status",
        timeout=_REQUEST_TIMEOUT_S,
    ).json()
    assert status["phase"] in ("simulated", "done")
    assert status["complete"] is True
    assert status["storyboard_approved"] is True
    assert status["output_asset_ids"] == []


def test_reaprobar_tras_el_terminal_es_replay_sin_efectos(live_approve_stack) -> None:
    """La tercera aprobación no vuelve a correr el lazo: el log no crece ni un evento."""
    production_id = _create_production(live_approve_stack, "replay idempotente")["production_id"]
    first = _approve(live_approve_stack, production_id).json()
    assert first["phase"] in ("simulated", "done")
    log_before = _get_plan(live_approve_stack, production_id)["log"]

    again = _approve(live_approve_stack, production_id)
    assert again.status_code == 200
    body = again.json()
    assert body["phase"] == first["phase"]
    assert body["final_output_path"] is None

    log_after = _get_plan(live_approve_stack, production_id)["log"]
    assert len(log_after) == len(log_before)
    assert _count_log(log_after, "approved") == 1
    assert _count_log(log_after, "execute") == _count_log(log_before, "execute")


def test_plan_y_cancelacion_bloqueados_tras_terminar(live_approve_stack) -> None:
    """Cerrada la puerta, no se reabre: editar el plan o cancelar un terminal es 409."""
    production_id = _create_production(live_approve_stack, "guards de estado")["production_id"]
    _approve(live_approve_stack, production_id)

    patch = httpx.patch(
        f"{live_approve_stack.base_url}/studio/api/productions/{production_id}/plan",
        json={"idea": "idea tarde"},
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert patch.status_code == 409

    cancel = httpx.post(
        f"{live_approve_stack.base_url}/studio/api/productions/{production_id}/cancel",
        timeout=_REQUEST_TIMEOUT_S,
    )
    assert cancel.status_code == 409


def test_checkpoint_sqlite_del_servidor_queda_en_una_fila_terminal(
    live_approve_stack,
) -> None:
    """El proceso servidor y el checkpointer real del test ven la misma BD: 1 fila terminal.

    Lee el SQLite del servidor desde este proceso con el componente real
    (otra conexión, mismo archivo WAL): confirma que la concurrencia HTTP dejó
    una única fila coherente para la producción, sin duplicados ni estados
    huérfanos en `executing`.
    """
    production_id = _create_production(live_approve_stack, "verificación por checkpoint")[
        "production_id"
    ]
    barrier = threading.Barrier(2)

    def _fire() -> httpx.Response:
        barrier.wait(timeout=20)
        return _approve(live_approve_stack, production_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_fire), pool.submit(_fire)]
        results = [f.result(timeout=60) for f in futures]
    assert all(r.status_code == 200 for r in results)

    cp = ProductionCheckpointer(db_path=live_approve_stack.checkpoint_db)
    state = cp.load(production_id)
    assert state is not None
    assert state.phase in ("simulated", "done")
    assert state.storyboard_approved is True
    assert _count_log(state.log, "approved") == 1
