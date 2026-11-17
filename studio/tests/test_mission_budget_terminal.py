"""Acceptance evidence for persisted mission spend and honest budget failures."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.orchestration import (
    Layer,
    ProductionCheckpointer,
    ProductionState,
    ProductionTerminalReason,
    Sequence,
    Shot,
    ShotStatus,
    assemble_reel,
    evaluate_and_replan,
    execute_next_shot,
)
from comfy_studio.orchestration.evaluator import StubShotEvaluator
from comfy_studio.orchestration.service import ProductionService


class ReportingRunner:
    def __init__(self, costs: list[float], output_dir: Path) -> None:
        self.costs = list(costs)
        self.output_dir = output_dir
        self.calls: list[tuple[str, float | None]] = []

    def run_layer(self, engine, job, *, cost_cap=None, cancel_event=None):
        self.calls.append((job["shot"], cost_cap))
        cost = self.costs.pop(0)
        output_path = self.output_dir / f"{job['shot']}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"accepted fixture output")
        return {
            "ok": True,
            "engine": engine,
            "output_path": str(output_path),
            "cost_usd": cost,
        }


def _shot(description: str, *, cap: float = 2.0) -> Shot:
    return Shot(
        description=description,
        cost_cap_usd=cap,
        acceptance={"vbench_min": 0.99},
        layers=[Layer(kind="gen", engine="comfyui")],
    )


def test_mission_spend_accumulates_across_replan_and_shots(tmp_path: Path) -> None:
    first = _shot("first")
    second = _shot("second")
    state = ProductionState(idea="production", cost_cap_usd=2.0)
    state.sequences.append(Sequence(title="sequence", shots=[first, second]))
    runner = ReportingRunner([0.2, 0.2, 0.3], tmp_path)

    execute_next_shot(state, runner)
    first.status = ShotStatus.EVALUATING
    evaluate_and_replan(state, StubShotEvaluator(score=0.1))
    execute_next_shot(state, runner)
    first.status = ShotStatus.ACCEPTED
    execute_next_shot(state, runner)

    assert state.cost_spent_usd == 0.7
    assert state.spend_reported is True
    assert [first.cost_spent_usd, second.cost_spent_usd] == [0.4, 0.3]
    status = ProductionService._status_dict(state)
    assert status["cost_cap_usd"] == 2.0
    assert status["cost_spent_usd"] == 0.7


def test_checkpoint_restart_replays_same_attempt_without_double_counting(tmp_path: Path) -> None:
    shot = _shot("replay")
    state = ProductionState(idea="replay", cost_cap_usd=1.0)
    state.sequences.append(Sequence(title="sequence", shots=[shot]))
    checkpointer = ProductionCheckpointer(tmp_path / "production.sqlite3")
    first_runner = ReportingRunner([0.4], tmp_path)

    class SimulatedProcessCrash(RuntimeError):
        pass

    def crash_after_spend_checkpoint(current: ProductionState) -> None:
        checkpointer.save(current)
        if current.spend_reported:
            raise SimulatedProcessCrash("simulated crash after spend was checkpointed")

    with pytest.raises(SimulatedProcessCrash):
        execute_next_shot(state, first_runner, checkpoint=crash_after_spend_checkpoint)
    resumed = checkpointer.load(state.id)
    assert resumed is not None
    assert resumed.cost_spent_usd == 0.4
    assert resumed.spend_reported is True

    replay_runner = ReportingRunner([0.4], tmp_path)
    execute_next_shot(resumed, replay_runner, checkpoint=checkpointer.save)

    assert len(replay_runner.calls) == 1
    assert first_runner.calls[0][0] == replay_runner.calls[0][0]
    assert resumed.cost_spent_usd == 0.4
    assert resumed.all_shots()[0].cost_spent_usd == 0.4
    assert len([event for event in resumed.log if event["kind"] == "layer_spend"]) == 1


def test_mission_cap_stops_next_shot_before_runner_and_persists_reason(
    tmp_path: Path,
) -> None:
    first = _shot("first")
    second = _shot("second")
    state = ProductionState(idea="budget", cost_cap_usd=0.5)
    state.sequences.append(Sequence(title="sequence", shots=[first, second]))
    runner = ReportingRunner([0.6], tmp_path)

    execute_next_shot(state, runner)
    first.status = ShotStatus.ACCEPTED
    execute_next_shot(state, runner)

    assert len(runner.calls) == 1
    assert runner.calls[0][1] == 0.5
    assert state.phase == "failed"
    assert state.terminal_reason == ProductionTerminalReason.BUDGET_EXHAUSTED


def test_legacy_checkpoint_defaults_new_mission_fields() -> None:
    shot = _shot("legacy")
    state = ProductionState(idea="legacy")
    state.sequences.append(Sequence(title="sequence", shots=[shot]))
    legacy = state.to_dict()
    legacy["schema_version"] = 2
    for field in ("cost_spent_usd", "spend_reported", "terminal_reason"):
        legacy.pop(field)
    shot_data = legacy["sequences"][0]["shots"][0]
    for field in ("cost_spent_usd", "spend_reported", "budget_exhausted"):
        shot_data.pop(field)

    restored = ProductionState.from_dict(legacy)

    assert restored.to_dict()["schema_version"] == 3
    assert restored.cost_spent_usd == 0.0
    assert restored.spend_reported is False
    assert restored.terminal_reason is None
    assert restored.all_shots()[0].budget_exhausted is False


def test_budget_exhausted_failure_never_assembles_as_done() -> None:
    shot = _shot("unfunded")
    shot.status = ShotStatus.FAILED
    shot.budget_exhausted = True
    state = ProductionState(idea="budget", cost_cap_usd=1.0, cost_spent_usd=1.0)
    state.sequences.append(Sequence(title="sequence", shots=[shot]))

    result = assemble_reel(state)

    assert result.phase == "failed"
    assert result.final_output_path is None
    assert result.terminal_reason == ProductionTerminalReason.BUDGET_EXHAUSTED


def test_executor_ladder_exhaustion_fails_production_with_typed_reason(
    tmp_path: Path,
) -> None:
    shot = _shot("over budget", cap=0.5)
    state = ProductionState(idea="budget ladder")
    state.sequences.append(Sequence(title="sequence", shots=[shot]))
    runner = ReportingRunner([0.4, 0.4], tmp_path)
    evaluator = StubShotEvaluator(score=0.1)

    execute_next_shot(state, runner)
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(state, evaluator)
    execute_next_shot(state, runner)
    shot.status = ShotStatus.EVALUATING
    evaluate_and_replan(state, evaluator)
    execute_next_shot(state, runner)
    execute_next_shot(state, runner)

    assert shot.status == ShotStatus.FAILED
    assert shot.budget_exhausted is True
    result = assemble_reel(state)
    assert result.phase == "failed"
    assert result.terminal_reason == ProductionTerminalReason.BUDGET_EXHAUSTED
