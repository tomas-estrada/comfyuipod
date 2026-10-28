"""Tests de recuperación de jobs interrumpidos (M3-B tarea 14, `recovery.py`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.recovery import (
    MAX_INTERRUPT_RETRIES,
    find_stale_jobs,
    recover_interrupted_jobs,
)
from comfy_studio.stubs.comfyui import ComfyUIStubState


@pytest.fixture()
def recovery_env(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
):
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(
        name="Proyecto Recovery",
        description="Jobs interrumpidos",
        aspect_ratio="16:9",
    )
    client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    return store, broker, project, client


def _create_stale_job(
    store: ProjectStore,
    project_id: str | None,
    *,
    kind: str = "render",
    template_id: str | None = "sdxl_image",
    status: str = "running",
    params: dict | None = None,
    result: dict | None = None,
) -> dict:
    job = store.create_job(
        kind=kind,
        project_id=project_id,
        template_id=template_id,
        params=params if params is not None else {"prompt": "un gato naranja"},
        status=status,
    )
    if result is not None:
        job = store.update_job(job["id"], result=result) or job
    return job


def test_find_stale_jobs_covers_in_flight_statuses(recovery_env) -> None:
    store, _broker, project, _client = recovery_env
    _create_stale_job(store, project["id"], status="running")
    _create_stale_job(store, project["id"], status="queued")
    done_job = _create_stale_job(store, project["id"], status="running")
    store.update_job(done_job["id"], status="done")

    stale = find_stale_jobs(store)
    statuses = sorted(j["status"] for j in stale)
    assert statuses == ["queued", "running"]


@pytest.mark.anyio
async def test_recover_resumes_render_job_once(
    recovery_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = recovery_env
    job = _create_stale_job(store, project["id"])

    report = await recover_interrupted_jobs(
        store=store,
        comfyui_client=client,
        broker=broker,
        resume=True,
        start_delay_s=0.0,
    )

    assert report["stale"] == 1
    assert report["requeued"] == 1
    assert report["done"] == 1
    assert report["interrupted"] == 0
    assert len(stub_comfyui_state.prompts) == 1

    recovered = store.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] == "done"

    gallery = [
        a
        for a in store.list_assets(project["id"], category="outputs")
        if a.get("job_id") == job["id"]
    ]
    assert gallery


@pytest.mark.anyio
async def test_recover_marks_exhausted_job_interrupted(
    recovery_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = recovery_env
    job = _create_stale_job(
        store,
        project["id"],
        result={"interrupt_retries": MAX_INTERRUPT_RETRIES},
    )

    report = await recover_interrupted_jobs(
        store=store,
        comfyui_client=client,
        broker=broker,
        resume=True,
    )

    assert report["interrupted"] == 1
    assert report["requeued"] == 0
    assert len(stub_comfyui_state.prompts) == 0
    recovered = store.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert "agotados" in (recovered["error"] or "")


@pytest.mark.anyio
async def test_recover_with_resume_disabled_marks_interrupted(
    recovery_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = recovery_env
    job = _create_stale_job(store, project["id"])

    report = await recover_interrupted_jobs(
        store=store,
        comfyui_client=client,
        broker=broker,
        resume=False,
    )

    assert report["interrupted"] == 1
    assert report["requeued"] == 0
    assert len(stub_comfyui_state.prompts) == 0
    assert store.get_job(job["id"])["status"] == "interrupted"


@pytest.mark.anyio
async def test_recover_skips_non_render_and_orphan_jobs(
    recovery_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = recovery_env
    other_kind = _create_stale_job(store, project["id"], kind="blender", status="running")
    no_template = _create_stale_job(store, project["id"], template_id=None, status="queued")

    report = await recover_interrupted_jobs(
        store=store,
        comfyui_client=client,
        broker=broker,
        resume=True,
    )

    assert report["stale"] == 2
    assert report["interrupted"] == 2
    assert report["requeued"] == 0
    assert len(stub_comfyui_state.prompts) == 0
    for job_id in (other_kind["id"], no_template["id"]):
        job = store.get_job(job_id)
        if job is not None:
            assert job["status"] == "interrupted"


@pytest.mark.anyio
async def test_recovered_job_that_fails_again_is_not_requeued_endlessly(
    recovery_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = recovery_env
    job = _create_stale_job(store, project["id"])

    first = await recover_interrupted_jobs(
        store=store, comfyui_client=client, broker=broker, resume=True
    )
    assert first["requeued"] == 1

    # Simular nueva caída: el job completado vuelve a estar en vuelo y el contador
    # de reintentos persiste (>= MAX) → la segunda recuperación NO reencola.
    store.update_job(
        job["id"],
        status="running",
        result={"interrupt_retries": MAX_INTERRUPT_RETRIES},
    )
    second = await recover_interrupted_jobs(
        store=store, comfyui_client=client, broker=broker, resume=True
    )
    assert second["requeued"] == 0
    assert second["interrupted"] == 1
    assert store.get_job(job["id"])["status"] == "interrupted"
