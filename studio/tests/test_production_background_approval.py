from __future__ import annotations

import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.service import ProductionService
from comfy_studio.routers.productions import register_routes


def test_background_approval_returns_job_and_duplicate_is_idempotent(tmp_path, monkeypatch):
    checkpointer = ProductionCheckpointer(tmp_path / "approval.sqlite3")
    service = ProductionService(checkpointer=checkpointer)
    production_id = service.plan_production("async approval")["production_id"]
    started = threading.Event()
    finish = threading.Event()

    def delayed_graph(state, current_id):
        started.set()
        assert finish.wait(3)
        state.phase = "simulated"
        checkpointer.save(state)
        return {"production_id": current_id, "phase": "simulated"}

    monkeypatch.setattr(service, "_run_approved_graph", delayed_graph)
    app = FastAPI()
    app.state.production_service = service
    register_routes(app)

    with TestClient(app) as client:
        response = client.post(f"/studio/api/productions/{production_id}/approve")
        assert response.status_code == 202
        body = response.json()
        assert body["production_id"] == production_id
        assert body["job_id"] == production_id
        assert body["status_url"].endswith(f"/{production_id}/status")
        assert body["cancel_url"].endswith(f"/{production_id}/cancel")
        assert started.wait(1)

        duplicate = client.post(f"/studio/api/productions/{production_id}/approve")
        assert duplicate.status_code == 202
        assert duplicate.json()["already_in_progress"] is True

        finish.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            status = client.get(f"/studio/api/productions/{production_id}/status").json()
            if status["phase"] == "simulated":
                break
            time.sleep(0.01)
        assert status["phase"] == "simulated"


def test_approval_queue_is_bounded_and_releases_slots_after_completion(tmp_path, monkeypatch):
    checkpointer = ProductionCheckpointer(tmp_path / "queue.sqlite3")
    service = ProductionService(checkpointer=checkpointer)
    started = threading.Event()
    finish = threading.Event()

    def delayed_graph(state, current_id):
        started.set()
        assert finish.wait(5)
        state.phase = "done"
        checkpointer.save(state)
        return {"production_id": current_id, "phase": "done"}

    monkeypatch.setattr(service, "_run_approved_graph", delayed_graph)
    ids = [service.plan_production(f"queued {i}")["production_id"] for i in range(9)]
    accepted = [service.approve_plan_background(pid) for pid in ids[:8]]
    assert len(accepted) == 8
    assert started.wait(1)

    from comfy_studio.orchestration.service import ProductionQueueFullError

    try:
        service.approve_plan_background(ids[8])
    except ProductionQueueFullError:
        pass
    else:
        raise AssertionError("the ninth concurrent approval must be rejected")

    finish.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if all(checkpointer.load(pid).phase == "done" for pid in ids[:8]):
            break
        time.sleep(0.01)
    assert all(checkpointer.load(pid).phase == "done" for pid in ids[:8])
    assert service.approve_plan_background(ids[8])["phase"] == "executing"


def test_startup_requeues_checkpoint_interrupted_by_process_restart(tmp_path, monkeypatch):
    checkpointer = ProductionCheckpointer(tmp_path / "restart.sqlite3")
    original = ProductionService(checkpointer=checkpointer)
    production_id = original.plan_production("resume after restart")["production_id"]
    state = checkpointer.load(production_id)
    assert state is not None
    state.phase = "executing"
    state.storyboard_approved = True
    state.metadata["execution_runtime_id"] = "old-process"
    checkpointer.save(state)

    restarted = ProductionService(checkpointer=checkpointer)
    started = threading.Event()

    def recovered_graph(current, current_id):
        started.set()
        current.phase = "simulated"
        checkpointer.save(current)
        return {"production_id": current_id, "phase": "simulated"}

    monkeypatch.setattr(restarted, "_run_approved_graph", recovered_graph)
    assert restarted.resume_interrupted_approvals() == [production_id]
    assert started.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        status = restarted.production_status(production_id)
        if status["phase"] == "simulated":
            break
        time.sleep(0.01)
    assert status["phase"] == "simulated"
