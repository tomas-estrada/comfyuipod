from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from comfy_studio.mission_compiler import (
    MissionBudgetExceeded,
    MissionCycleError,
    MissionValidationError,
    compile_mission,
    topological_task_order,
)
from comfy_studio.orchestration import (
    Collaborators,
    ProductionCheckpointer,
    ProductionService,
)


def _brief(*, duration: float = 15.0) -> dict:
    return {
        "schema": "comfy_studio.brief.v1",
        "project_id": "project-1",
        "prompt": "Un reel de un producto, sin música, para Instagram",
        "status": "approved",
        "purpose": "Presentar el producto con evidencia",
        "main_message": "Producto artesanal",
        "piece_type": "product",
        "audience": {"hypothesis": "Personas interesadas en diseño", "is_hypothesis": True},
        "duration": {"target_sec": duration, "min_sec": duration * 0.9, "max_sec": duration * 1.1},
        "aspect_ratio": "9:16",
        "user_constraints": [
            {"key": "music", "value": False, "text": "sin música", "source": "user"},
            {"key": "aspect_ratio", "value": "9:16", "text": "vertical", "source": "user"},
        ],
        "asset_facts": [
            {
                "id": "fact-1",
                "asset_id": "asset-1",
                "kind": "transcript_segment",
                "simulated": False,
            }
        ],
        "assumptions": [{"text": "Se propone un cierre directo", "basis": "formato reel"}],
        "editorial": {"promise": "Mostrar el producto", "concept": "Demostración"},
        "acceptance_criteria": [{"id": "ac-1", "check": "No inventar hechos"}],
        "budget": {"cost_cap_usd": 5.0},
        "created_at": "2026-11-14T00:00:00+00:00",
    }


def test_compiles_brief_to_bounded_provenance_carrying_dag() -> None:
    result = compile_mission(_brief())

    assert result.schema == "comfy_studio.mission.v1"
    assert len(result.hypotheses) >= 1
    assert result.hypotheses[0].source_path == "audience.hypothesis"
    assert result.constraints[0].source_path == "user_constraints[0]"
    assert result.source_refs
    assert len(result.tasks) == 3
    assert result.tasks[0].depends_on == ()
    assert result.tasks[1].depends_on == (result.tasks[0].task_id,)
    assert result.tasks[2].depends_on == (result.tasks[1].task_id,)
    assert topological_task_order(result.tasks) == tuple(t.task_id for t in result.tasks)
    assert result.limits.max_shots >= len(result.tasks)


def test_compile_is_stable_when_only_volatile_brief_timestamp_changes() -> None:
    first = _brief()
    second = copy.deepcopy(first)
    second["created_at"] = "2026-11-15T00:00:00+00:00"

    left = compile_mission(first)
    right = compile_mission(second)

    assert left.idempotency_key == right.idempotency_key
    assert [t.task_id for t in left.tasks] == [t.task_id for t in right.tasks]
    assert left.to_dict() == right.to_dict()


def test_rejects_invalid_brief_with_typed_validation_error() -> None:
    invalid = _brief()
    invalid["schema"] = "unknown.v2"

    with pytest.raises(MissionValidationError, match="schema"):
        compile_mission(invalid)


def test_rejects_unapproved_brief() -> None:
    draft = _brief()
    draft["status"] = "draft"

    with pytest.raises(MissionValidationError, match="aprobado"):
        compile_mission(draft)


def test_rejects_brief_whose_duration_exceeds_shot_budget() -> None:
    with pytest.raises(MissionBudgetExceeded, match="max_shots"):
        compile_mission(_brief(duration=65.0))


def test_rejects_compiled_prompt_that_would_be_truncated() -> None:
    oversized = _brief()
    oversized["user_constraints"][0]["value"] = "x" * 5000

    with pytest.raises(MissionBudgetExceeded, match="max_prompt_chars"):
        compile_mission(oversized)


def test_topological_order_reports_cycles_as_typed_errors() -> None:
    tasks = [
        {"task_id": "a", "depends_on": ["b"]},
        {"task_id": "b", "depends_on": ["a"]},
    ]

    with pytest.raises(MissionCycleError, match="ciclo"):
        topological_task_order(tasks)


def test_unknown_dependency_is_a_typed_validation_error() -> None:
    with pytest.raises(MissionValidationError, match="dependencia"):
        topological_task_order([{"task_id": "a", "depends_on": ["missing"]}])


def test_production_service_uses_compiled_dag_and_deduplicates_retries(tmp_path) -> None:
    class PlannerMustNotRun:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("brief compilation must not call an LLM planner")

    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "missions.sqlite3"),
        collaborators=Collaborators(planner=PlannerMustNotRun()),
    )
    brief = _brief()

    first = service.plan_production(
        brief["prompt"], brief=brief, project_id=brief["project_id"]
    )
    second = service.plan_production(
        brief["prompt"], brief=copy.deepcopy(brief), project_id=brief["project_id"]
    )

    first_plan = first["plan"]
    second_plan = second["plan"]
    assert first["production_id"] == second["production_id"]
    assert first_plan["id"] == second_plan["id"]
    receipt = first_plan["metadata"]["mission_compilation"]
    assert receipt["schema"] == "comfy_studio.mission.v1"
    assert receipt["task_ids"] == [
        shot["id"]
        for sequence in first_plan["sequences"]
        for shot in sequence["shots"]
    ]
    assert len(first_plan["sequences"][0]["shots"]) == 3
    assert first_plan["phase"] == "awaiting_approval"
    first_shot = first_plan["sequences"][0]["shots"][0]
    layer = first_shot["layers"][0]
    assert "music=false" in layer["params"]["prompt"]
    assert "audience.hypothesis" in layer["params"]["source_refs"]
    assert "user_constraints[0]" in layer["params"]["source_refs"]
    assert first_plan["metadata"]["mission_compilation"]["constraints"][0]["key"] == "music"


def test_same_brief_can_be_replanned_after_preapproval_cancellation(tmp_path) -> None:
    class PlannerMustNotRun:
        def plan(self, *_args, **_kwargs):
            raise AssertionError("brief compilation must not call an LLM planner")

    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "missions.sqlite3"),
        collaborators=Collaborators(planner=PlannerMustNotRun()),
    )
    brief = _brief()

    first = service.plan_production(brief["prompt"], brief=brief)
    service.cancel_production(first["production_id"])
    second = service.plan_production(brief["prompt"], brief=brief)

    assert second["production_id"] != first["production_id"]
    assert second["phase"] == "awaiting_approval"
    assert service.approve_plan(second["production_id"])["phase"] == "simulated"


def test_mission_compiler_engine_evidence_matches_locked_sources() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    manifest = json.loads(
        (repo_root / "upgrades-ultra-version/engines.json").read_text(encoding="utf-8")
    )
    engine = next(item for item in manifest["engines"] if item["id"] == "mission-compiler")
    assert engine["implementation_status"] == "implemented"
    assert engine["evidence_entrypoint"] == "studio/comfy_studio/mission_compiler.py"
    assert engine["evidence_test"] == "studio/tests/test_mission_compiler.py"
    lock_name, expected_lock_hash = next(iter(engine["dependency_lock_hashes"].items()))
    lock_path = repo_root / lock_name
    assert hashlib.sha256(lock_path.read_bytes()).hexdigest() == expected_lock_hash
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assert lock["source_revision"] == engine["code_revision"]
    for source_name, expected_hash in lock["pinned_source_sha256"].items():
        pinned_bytes = subprocess.run(
            ["git", "-C", str(repo_root), "show", f"{engine['code_revision']}:{source_name}"],
            check=True,
            capture_output=True,
        ).stdout
        assert hashlib.sha256(pinned_bytes).hexdigest() == expected_hash
    for source_name, expected_hash in lock["integration_source_sha256"].items():
        assert hashlib.sha256((repo_root / source_name).read_bytes()).hexdigest() == expected_hash
    test_evidence = lock["verification_test"]
    assert test_evidence["path"] == engine["evidence_test"]
    assert hashlib.sha256((repo_root / test_evidence["path"]).read_bytes()).hexdigest() == (
        test_evidence["sha256"]
    )
