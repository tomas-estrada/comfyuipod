"""CPU-only recovery checks for durable production execution checkpoints."""

from __future__ import annotations

from threading import Event

import pytest

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.executor import execute_next_shot
from comfy_studio.orchestration.graph import END, ProductionGraph
from comfy_studio.orchestration.service import ProductionService
from comfy_studio.orchestration.state import Layer, ProductionState, Sequence, Shot, ShotStatus
from comfy_studio.projects import ProjectStore


def test_graph_restart_resumes_inflight_node_without_replaying_completed_nodes(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "graph.sqlite3")
    state = ProductionState(idea="resume between stages")
    completed: list[str] = []
    attempts = 0

    def prepare(current: ProductionState) -> ProductionState:
        completed.append("prepare")
        current.metadata["prepared"] = True
        return current

    def render(current: ProductionState) -> ProductionState:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("process stopped during render")
        completed.append("render")
        current.phase = "done"
        return current

    def make_graph() -> ProductionGraph:
        graph = ProductionGraph(checkpointer=checkpointer)
        graph.add_node("prepare", prepare)
        graph.add_node("render", render)
        graph.set_entry("prepare")
        graph.set_router(
            lambda current: END if current.phase == "done"
            else "render" if current.metadata.get("prepared")
            else "prepare"
        )
        return graph

    with pytest.raises(RuntimeError, match="stopped"):
        make_graph().run(state)

    interrupted = checkpointer.load(state.id)
    assert interrupted is not None
    assert interrupted.metadata["_graph_inflight_node"] == "render"
    final = make_graph().run(interrupted)

    assert final.phase == "done"
    assert completed == ["prepare", "render"]
    assert attempts == 2


def test_executor_checkpoints_each_layer_and_reuses_idempotency_key(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "layers.sqlite3")
    shot = Shot(
        description="two layer render",
        layers=[Layer(kind="gen", engine="engine-a"), Layer(kind="upscale", engine="engine-b")],
    )
    state = ProductionState(idea="recover layer results")
    state.sequences = [Sequence(title="one", shots=[shot])]
    calls: list[tuple[str, str]] = []

    class Runner:
        fail_once = True

        def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
            key = str(job["idempotency_key"])
            calls.append((engine, key))
            if engine == "engine-b" and self.fail_once:
                self.fail_once = False
                raise RuntimeError("simulated process interruption")
            out = tmp_path / f"{engine}.out"
            out.write_bytes(b"artifact\n")
            return {"ok": True, "output_path": str(out)}

    runner = Runner()
    with pytest.raises(RuntimeError, match="interruption"):
        execute_next_shot(state, runner, checkpoint=checkpointer.save)

    interrupted = checkpointer.load(state.id)
    assert interrupted is not None
    assert interrupted.all_shots()[0].status == ShotStatus.RUNNING
    assert interrupted.all_shots()[0].layers[0].output_path == str(tmp_path / "engine-a.out")
    resumed = execute_next_shot(interrupted, runner, checkpoint=checkpointer.save)

    assert resumed.all_shots()[0].status == ShotStatus.EVALUATING
    assert [engine for engine, _ in calls] == ["engine-a", "engine-b", "engine-b"]
    assert calls[1][1] == calls[2][1]


def test_replan_attempt_does_not_reuse_previous_layer_artifacts():
    shot = Shot(
        description="replanned render",
        layers=[Layer(kind="gen", engine="engine-a", output_path="/tmp/old.out")],
        output_path="/tmp/old.out",
    )
    shot.status = ShotStatus.EVALUATING

    assert shot.escalate_replan() is not None
    assert shot.layers[0].output_path is None
    assert shot.output_path is None


def test_approve_reclaims_orphaned_executing_checkpoint(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "service.sqlite3")
    original = ProductionService(checkpointer=checkpointer)
    production_id = original.plan_production("resume approved work")["production_id"]
    interrupted = checkpointer.load(production_id)
    assert interrupted is not None
    interrupted.phase = "executing"
    interrupted.storyboard_approved = True
    interrupted.metadata["_graph_inflight_node"] = "execute"
    interrupted.record("approved")
    checkpointer.save(interrupted)

    restarted = ProductionService(checkpointer=checkpointer)
    result = restarted.approve_plan(production_id)

    assert result["phase"] == "simulated"
    assert checkpointer.load(production_id).phase == "simulated"


def test_approve_retry_during_live_execution_is_idempotent(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "active.sqlite3")
    service = ProductionService(checkpointer=checkpointer)
    production_id = service.plan_production("do not launch twice")["production_id"]
    state = checkpointer.load(production_id)
    assert state is not None
    state.phase = "executing"
    checkpointer.save(state)
    service._cancel_events[production_id] = Event()

    result = service.approve_plan(production_id)

    assert result["already_in_progress"] is True
    assert result["phase"] == "executing"


def test_approve_retry_during_graph_registration_window_is_idempotent(tmp_path):
    checkpointer = ProductionCheckpointer(tmp_path / "registration.sqlite3")
    service = ProductionService(checkpointer=checkpointer)
    production_id = service.plan_production("do not race graph registration")["production_id"]
    state = checkpointer.load(production_id)
    assert state is not None
    state.phase = "executing"
    state.metadata["execution_runtime_id"] = service._runtime_id
    checkpointer.save(state)

    result = service.approve_plan(production_id)

    assert result["already_in_progress"] is True
    assert production_id not in service._cancel_events


def test_gallery_finalization_is_idempotent_after_graph_completion(tmp_path):
    store = ProjectStore(tmp_path / "studio")
    project = store.create_project(name="Reanudación de artefactos")
    shot_file = tmp_path / "shot.mp4"
    reel_file = tmp_path / "reel.mp4"
    shot_file.write_bytes(b"shot-video")
    reel_file.write_bytes(b"reel-video")
    shot = Shot(description="shot de prueba", status=ShotStatus.ACCEPTED)
    shot.output_path = str(shot_file)
    state = ProductionState(idea="reel de prueba")
    state.sequences = [Sequence(title="única", shots=[shot])]
    state.phase = "done"
    state.final_output_path = str(reel_file)
    state.metadata["project_id"] = project["id"]
    state.metadata["execution_mode"] = "real"
    state.metadata["artifact_verified"] = True
    checkpointer = ProductionCheckpointer(tmp_path / "checkpoints.sqlite3")
    service = ProductionService(
        checkpointer=checkpointer,
        store=store,
    )
    checkpointer.save(state)

    first = service.approve_plan(state.id)
    second = service.approve_plan(state.id)

    assert second["output_asset_ids"] == first["output_asset_ids"]
    assert len(first["output_asset_ids"]) == 2
    assert len(store.list_assets(project["id"], category="outputs")) == 2
