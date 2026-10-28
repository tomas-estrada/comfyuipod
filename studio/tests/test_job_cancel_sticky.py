"""Regresión: un job `cancelled` no debe ser sobrescrito por eventos tardíos de ComfyUI."""

from __future__ import annotations

from pathlib import Path

from comfy_studio.projects import ProjectStore


def _make_job(store: ProjectStore) -> str:
    project = store.create_project("sticky-cancel")
    job = store.create_job(
        kind="render", project_id=project["id"], template_id="t", status="running"
    )
    return str(job["id"])


def test_cancelled_job_ignores_late_status_updates(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    job_id = _make_job(store)

    store.update_job(job_id, status="cancelled", error="Cancelado por el agente (job_cancel).")

    for late_status in ("running", "done", "error", None):
        after = store.update_job(
            job_id,
            status=late_status,
            progress_value=3,
            error="evento tardío",
        )
        assert after is not None
        assert after["status"] == "cancelled"
        assert after["error"] == "Cancelado por el agente (job_cancel)."

    assert store.get_job(job_id)["status"] == "cancelled"  # type: ignore[index]


def test_non_cancelled_job_transitions_still_work(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "data")
    job_id = _make_job(store)

    done = store.update_job(job_id, status="done", progress_value=1)
    assert done is not None
    assert done["status"] == "done"


def test_client_complete_and_fail_ignore_cancelled_jobs(tmp_path: Path) -> None:
    from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError, PromptResult
    from comfy_studio.progress import ProgressBroker

    store = ProjectStore(tmp_path / "data")
    job_id = _make_job(store)
    store.update_job(job_id, status="cancelled")

    broker = ProgressBroker()
    events = []
    broker.publish = lambda t, d: events.append((t, d)) or d  # type: ignore[assignment]

    client = ComfyUIClient(store=store, broker=broker)

    # Calling _complete_job should do nothing
    res = PromptResult(prompt_id="p1", outputs=[], status="done")
    client._complete_job(res, job_id=job_id)
    assert store.get_job(job_id)["status"] == "cancelled"  # type: ignore[index]
    assert len(events) == 0

    # Calling _fail_job should do nothing
    err = ComfyUIError("Failed!")
    client._fail_job(err, job_id=job_id)
    assert store.get_job(job_id)["status"] == "cancelled"  # type: ignore[index]
    assert len(events) == 0

