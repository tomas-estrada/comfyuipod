"""Tests del watchdog de reconciliación en vivo, del guard de disco en subidas y
de `poll_until_complete` ante prompts perdidos (ComfyUI reiniciado, OOM 137).

Cubre:
- `recover_interrupted_jobs(watchdog=True)` no re-ejecuta prompts vivos en ComfyUI
  (`active`) ni toca jobs sin `prompt_id`; los prompts perdidos se reencolan (`requeued`).
- Arranque (`watchdog=False`) con prompt vivo: `attached` + re-adjunte vía poll.
- `poll_until_complete` marca el job `interrupted` cuando el prompt desaparece de
  `/queue` e `/history` (no espera al timeout de 300 s ni lo marca `error`).
- `_check_upload_free_disk` devuelve 507 cuando el volumen no tiene sitio.
- `STUDIO_RECONCILE_INTERVAL_S` se parsea y 0 desactiva el watchdog.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.datastructures import Headers

from comfy_studio.app import _check_upload_free_disk
from comfy_studio.comfyui_client import (
    ComfyUIClient,
    ComfyUIConnectionError,
    ComfyUIExecutionError,
    ComfyUITimeoutError,
)
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.recovery import recover_interrupted_jobs
from comfy_studio.stubs.comfyui import ComfyUIStubState


@pytest.fixture()
def reconcile_env(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
):
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Reconcile", aspect_ratio="16:9")
    client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
        poll_interval=0.01,
    )
    return store, broker, project, client


def _job_with_prompt(store: ProjectStore, project_id: str, prompt_id: str) -> dict[str, Any]:
    job = store.create_job(
        kind="render",
        project_id=project_id,
        template_id="sdxl_image",
        params={"prompt": "un gato naranja"},
        status="running",
        prompt_id=prompt_id,
    )
    return job


@pytest.mark.anyio
async def test_watchdog_leaves_live_prompt_alone(
    reconcile_env,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    store, broker, project, client = reconcile_env
    submission = await client.submit_prompt({"1": {"class_type": "Empty"}})
    job = _job_with_prompt(store, project["id"], submission.prompt_id)
    # Job en cola sin prompt_id todavía: el watchdog no debe tocarlo
    pending = store.create_job(kind="render", project_id=project["id"], status="queued")

    report = await recover_interrupted_jobs(
        store=store, comfyui_client=client, broker=broker, resume=True, watchdog=True
    )

    assert report["active"] == 1
    assert report["requeued"] == 0
    assert report["attached"] == 0
    assert len(stub_comfyui_state.prompts) == 1  # no se re-encoló nada
    assert store.get_job(job["id"])["status"] == "running"
    assert store.get_job(pending["id"])["status"] == "queued"


@pytest.mark.anyio
async def test_watchdog_requeues_lost_prompt(reconcile_env) -> None:
    store, broker, project, client = reconcile_env
    job = _job_with_prompt(store, project["id"], "ghost-prompt-123")

    report = await recover_interrupted_jobs(
        store=store, comfyui_client=client, broker=broker, resume=True, watchdog=True
    )

    assert report["requeued"] == 1
    assert report["done"] == 1
    assert store.get_job(job["id"])["status"] == "done"
    # Se re-encoló con un prompt_id nuevo, distinto del perdido
    assert store.get_job(job["id"])["prompt_id"] != "ghost-prompt-123"


@pytest.mark.anyio
async def test_boot_reattaches_live_prompt(reconcile_env) -> None:
    store, broker, project, client = reconcile_env
    submission = await client.submit_prompt({"1": {"class_type": "Empty"}})
    job = _job_with_prompt(store, project["id"], submission.prompt_id)

    report = await recover_interrupted_jobs(
        store=store, comfyui_client=client, broker=broker, resume=True, watchdog=False
    )

    assert report["attached"] == 1
    assert report["requeued"] == 0
    assert store.get_job(job["id"])["result"].get("reattached_after_restart") is True
    # Dejar terminar la tarea de re-adjunte lanzada en segundo plano
    await asyncio.sleep(0.2)
    assert store.get_job(job["id"])["status"] == "done"


@pytest.mark.anyio
async def test_poll_marks_interrupted_when_prompt_vanishes(reconcile_env) -> None:
    store, _broker, project, client = reconcile_env
    job = _job_with_prompt(store, project["id"], "lost-prompt-999")

    with pytest.raises(ComfyUIExecutionError):
        await client.poll_until_complete(
            "lost-prompt-999",
            poll_interval=0.01,
            timeout=30.0,
            job_id=job["id"],
            project_id=project["id"],
        )

    recovered = store.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] == "interrupted"
    assert "desapareció" in (recovered["error"] or "")


@pytest.mark.anyio
async def test_poll_does_not_mark_interrupted_on_transport_errors(
    reconcile_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7-SCR-004: un error de transporte no prueba que el prompt desapareció.

    Con ComfyUI inalcanzable (reinicio en curso o cuelgue transitorio) el poll NO
    debe contar ticks como "ausente" ni marcar el job `interrupted`; espera al
    timeout y marca el fallo como timeout.
    """
    store, _broker, project, client = reconcile_env
    job = _job_with_prompt(store, project["id"], "flaky-prompt-1")

    async def _boom(*_args, **_kwargs):
        raise ComfyUIConnectionError("ComfyUI no responde")

    monkeypatch.setattr(client, "get_history", _boom)
    monkeypatch.setattr(client, "get_queue", _boom)

    with pytest.raises(ComfyUITimeoutError):
        await client.poll_until_complete(
            "flaky-prompt-1",
            poll_interval=0.01,
            timeout=0.2,
            job_id=job["id"],
            project_id=project["id"],
        )

    recovered = store.get_job(job["id"])
    assert recovered is not None
    assert recovered["status"] != "interrupted"
    assert "desapareció" not in (recovered["error"] or "")


def test_upload_free_disk_returns_507_when_full(
    studio_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import comfy_studio.app as app_module

    monkeypatch.setattr(
        app_module.shutil, "disk_usage", lambda _p: _FakeUsage(free=100)
    )
    headers = Headers({"Content-Length": "1000000"})
    with pytest.raises(HTTPException) as excinfo:
        _check_upload_free_disk(headers, studio_data_dir)
    assert excinfo.value.status_code == 507


def test_upload_free_disk_passes_with_room(
    studio_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import comfy_studio.app as app_module

    monkeypatch.setattr(
        app_module.shutil,
        "disk_usage",
        lambda _p: _FakeUsage(free=10 * 1024**3),
    )
    headers = Headers({"Content-Length": "1000"})
    _check_upload_free_disk(headers, studio_data_dir)  # no lanza


class _FakeUsage:
    def __init__(self, *, free: int) -> None:
        self.free = free
        self.total = free + 10**12
        self.used = 10**12


def test_reconcile_interval_env_parsing() -> None:
    cfg = StudioConfig.from_env({})
    assert cfg.studio_reconcile_interval_s == 30.0
    cfg = StudioConfig.from_env({"STUDIO_RECONCILE_INTERVAL_S": "0"})
    assert cfg.studio_reconcile_interval_s == 0.0
    cfg = StudioConfig.from_env({"STUDIO_RECONCILE_INTERVAL_S": "5"})
    assert cfg.studio_reconcile_interval_s == 5.0
    cfg = StudioConfig.from_env({"STUDIO_RECONCILE_INTERVAL_S": "basura"})
    assert cfg.studio_reconcile_interval_s == 30.0
