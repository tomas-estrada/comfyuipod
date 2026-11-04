"""M7-SCR-001: submit de plantillas asíncrono (202 Accepted + ejecución en segundo plano).

- Por defecto `POST /studio/api/templates/{id}/submit` devuelve 202 con el `job_id` y
  el render corre en una tarea asyncio (semáforo `STUDIO_MAX_CONCURRENT_RENDERS`);
  el resultado se sigue por `GET /studio/api/jobs/{job_id}` y SSE.
- `?wait=1` recupera el contrato síncrono (200 + resultado completo).
- Los errores de validación (params/assets/plantilla/pesos) siguen devolviendo 4xx en
  el encolado, ANTES de crear ningún job ni enviar nada a ComfyUI.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from comfy_studio.stubs.comfyui import ComfyUIStubState

TERMINAL_JOB_STATUSES = {"done", "error", "failed", "cancelled", "interrupted"}


def _wait_terminal_job(client: TestClient, job_id: str, timeout_s: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        resp = client.get(f"/studio/api/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = resp.json()
        if job["status"] in TERMINAL_JOB_STATUSES:
            return job
        time.sleep(0.1)
    raise AssertionError(f"El job {job_id} no llegó a estado terminal en {timeout_s}s")


def _new_project(client: TestClient, name: str) -> str:
    resp = client.post("/studio/api/projects", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_submit_async_returns_202_and_completes_in_background(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_id = _new_project(client, "Async Submit 202")
    resp = client.post(
        "/studio/api/templates/sdxl_image/submit",
        json={"project_id": project_id, "params": {"prompt": "gato astronauta neon"}},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["async"] is True
    assert body["status"] == "queued"
    assert body["template_id"] == "sdxl_image"
    assert body["project_id"] == project_id
    assert body["location"] == f"/studio/api/jobs/{body['job_id']}"
    assert body["progress_url"] == "/studio/api/progress"

    # El render termina en segundo plano y queda en done con prompt del stub.
    job = _wait_terminal_job(client, body["job_id"])
    assert job["status"] == "done", job
    assert job["prompt_id"] and job["prompt_id"].startswith("stub-")

    # La salida se registró en la galería del proyecto con linaje del job.
    gallery = client.get(
        f"/studio/api/projects/{project_id}/gallery?category=outputs"
    ).json()
    assert any(a.get("job_id") == body["job_id"] for a in gallery["assets"])


def test_submit_wait1_keeps_sync_contract_200_with_full_result(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_id = _new_project(client, "Sync Wait1")
    resp = client.post(
        "/studio/api/templates/sdxl_image/submit?wait=1",
        json={"project_id": project_id, "params": {"prompt": "perro samurai"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "done"
    assert body["prompt_id"].startswith("stub-")
    assert body["template_id"] == "sdxl_image"
    assert isinstance(body["workflow"], dict) and body["workflow"]
    assert isinstance(body["outputs"], list)


def test_submit_async_invalid_params_returns_4xx_and_never_reaches_comfyui(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_id = _new_project(client, "Async Invalid")
    resp = client.post(
        "/studio/api/templates/sdxl_image/submit",
        json={"project_id": project_id, "params": {"prompt": "   "}},
    )
    assert 400 <= resp.status_code < 500, resp.text
    assert "prompt" in resp.text.lower()
    assert len(stub_comfyui_state.prompts) == 0
    jobs = client.get(f"/studio/api/jobs?project_id={project_id}").json()["jobs"]
    assert jobs == []


def test_submit_async_unknown_template_404(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    resp = client.post(
        "/studio/api/templates/no_existe/submit",
        json={"params": {"prompt": "hola"}},
    )
    assert resp.status_code == 404
    assert len(stub_comfyui_state.prompts) == 0


def test_submit_async_project_scoped_route_also_202(
    client: TestClient,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    project_id = _new_project(client, "Async Project Route")
    resp = client.post(
        f"/studio/api/projects/{project_id}/templates/sdxl_image/submit",
        json={"params": {"prompt": "ciudad bajo el mar"}},
    )
    assert resp.status_code == 202, resp.text
    job = _wait_terminal_job(client, resp.json()["job_id"])
    assert job["status"] == "done"
    assert job["project_id"] == project_id
