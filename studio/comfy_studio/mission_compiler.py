"""Deterministic, CPU-only compiler from a versioned creative brief to a DAG.

The compiler is stateless. ProductionState remains the only persisted
production authority; orchestration converts the typed result into sequences
and shots already owned by that state.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from comfy_studio.creative.brief import validate_brief

MISSION_SCHEMA = "comfy_studio.mission.v1"
_DEFAULT_SHOT_SECONDS = 5.0
_MIN_SHOT_SECONDS = 3.0


class MissionCompileError(ValueError):
    """Base class for typed mission compilation failures."""


class MissionValidationError(MissionCompileError):
    """The input brief or task graph does not satisfy its contract."""


class MissionBudgetExceeded(MissionCompileError):
    """The request exceeds a declared compilation limit."""


class MissionCycleError(MissionCompileError):
    """The task dependency graph contains a cycle."""


@dataclass(frozen=True, slots=True)
class MissionLimits:
    """Hard bounds that keep compilation work and output finite."""

    max_input_bytes: int = 256_000
    max_prompt_chars: int = 4000
    max_hypotheses: int = 16
    max_constraints: int = 64
    max_asset_facts: int = 64
    max_shots: int = 12
    max_cost_cap_usd: float = 1000.0

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_input_bytes": self.max_input_bytes,
            "max_prompt_chars": self.max_prompt_chars,
            "max_hypotheses": self.max_hypotheses,
            "max_constraints": self.max_constraints,
            "max_asset_facts": self.max_asset_facts,
            "max_shots": self.max_shots,
            "max_cost_cap_usd": self.max_cost_cap_usd,
        }


DEFAULT_LIMITS = MissionLimits()


@dataclass(frozen=True, slots=True)
class MissionHypothesis:
    hypothesis_id: str
    text: str
    source_path: str
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.hypothesis_id,
            "text": self.text,
            "source_path": self.source_path,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class MissionConstraint:
    key: str
    value: Any
    source_path: str
    source_text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "source_path": self.source_path,
            "source_text": self.source_text,
        }


@dataclass(frozen=True, slots=True)
class MissionTask:
    task_id: str
    title: str
    description: str
    duration_s: float
    depends_on: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "duration_s": self.duration_s,
            "depends_on": list(self.depends_on),
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True, slots=True)
class MissionCompilation:
    schema: str
    idempotency_key: str
    hypotheses: tuple[MissionHypothesis, ...]
    constraints: tuple[MissionConstraint, ...]
    source_refs: tuple[dict[str, Any], ...]
    tasks: tuple[MissionTask, ...]
    cost_cap_usd: float
    limits: MissionLimits

    def to_dict(self) -> dict[str, Any]:
        """Return the complete versioned compilation value."""
        return {
            "schema": self.schema,
            "idempotency_key": self.idempotency_key,
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "constraints": [item.to_dict() for item in self.constraints],
            "source_refs": [dict(item) for item in self.source_refs],
            "tasks": [item.to_dict() for item in self.tasks],
            "budget": {
                "cost_cap_usd": self.cost_cap_usd,
                "max_shots": self.limits.max_shots,
            },
            "limits": self.limits.to_dict(),
        }

    def receipt(self) -> dict[str, Any]:
        """Checkpoint provenance only; the mutable DAG stays in ProductionState."""
        graph_json = _canonical_json([task.to_dict() for task in self.tasks])
        return {
            "schema": self.schema,
            "idempotency_key": self.idempotency_key,
            "graph_sha256": hashlib.sha256(graph_json.encode("utf-8")).hexdigest(),
            "task_ids": [task.task_id for task in self.tasks],
            "hypotheses": [item.to_dict() for item in self.hypotheses],
            "constraints": [item.to_dict() for item in self.constraints],
            "source_refs": [dict(item) for item in self.source_refs],
            "budget": {
                "cost_cap_usd": self.cost_cap_usd,
                "max_shots": self.limits.max_shots,
            },
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_brief(brief: dict[str, Any]) -> str:
    stable = dict(brief)
    stable.pop("created_at", None)
    return _canonical_json(stable)


def topological_task_order(
    tasks: Sequence[MissionTask | Mapping[str, Any]],
) -> tuple[str, ...]:
    """Validate and return a stable topological order for task mappings."""
    ids: list[str] = []
    dependencies: dict[str, tuple[str, ...]] = {}
    for task in tasks:
        task_id = task.task_id if isinstance(task, MissionTask) else task.get("task_id")
        raw_deps = (
            task.depends_on
            if isinstance(task, MissionTask)
            else task.get("depends_on", ())
        )
        if not isinstance(task_id, str) or not task_id.strip():
            raise MissionValidationError("task_id debe ser texto no vacío")
        if task_id in dependencies:
            raise MissionValidationError(f"task_id duplicado: {task_id}")
        if not isinstance(raw_deps, (list, tuple)):
            raise MissionValidationError(f"depends_on inválido para {task_id}")
        deps = tuple(raw_deps)
        if any(not isinstance(dep, str) for dep in deps):
            raise MissionValidationError(f"dependencia inválida para {task_id}")
        ids.append(task_id)
        dependencies[task_id] = deps
    for task_id, deps in dependencies.items():
        missing = [dep for dep in deps if dep not in dependencies]
        if missing:
            raise MissionValidationError(
                f"dependencia desconocida para {task_id}: {missing[0]}"
            )

    dependents: dict[str, list[str]] = {task_id: [] for task_id in ids}
    indegree = {task_id: len(deps) for task_id, deps in dependencies.items()}
    for task_id, deps in dependencies.items():
        for dependency in deps:
            dependents[dependency].append(task_id)
    ready = deque(task_id for task_id in ids if indegree[task_id] == 0)
    ordered: list[str] = []
    while ready:
        task_id = ready.popleft()
        ordered.append(task_id)
        for dependent in dependents[task_id]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(ordered) != len(ids):
        cyclic = next(task_id for task_id in ids if indegree[task_id] > 0)
        raise MissionCycleError(f"el DAG de misiones tiene un ciclo en {cyclic}")
    return tuple(ordered)


def compile_mission(
    brief: dict[str, Any],
    *,
    cost_cap_usd: float | None = None,
    limits: MissionLimits = DEFAULT_LIMITS,
) -> MissionCompilation:
    """Compile a validated creative brief into hypotheses, constraints and a DAG.

    No model, network, database, or mutable service state is consulted. Only
    user-authored constraints become hard constraints; assumptions remain
    hypotheses and asset facts are referenced by stable IDs.
    """
    if not isinstance(brief, dict):
        raise MissionValidationError("El brief debe ser un objeto JSON")
    try:
        serialized = _canonical_json(brief)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError(
            "El brief debe ser JSON finito y serializable"
        ) from exc
    if len(serialized.encode("utf-8")) > limits.max_input_bytes:
        raise MissionBudgetExceeded("El brief excede max_input_bytes")
    try:
        validate_brief(brief)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError(str(exc)) from exc
    if brief.get("status") != "approved":
        raise MissionValidationError("El brief debe estar aprobado antes de compilar")
    if not isinstance(brief.get("prompt"), str) or not brief["prompt"].strip():
        raise MissionValidationError("El brief requiere prompt no vacío")
    if not isinstance(brief.get("project_id"), str) or not brief["project_id"].strip():
        raise MissionValidationError("El brief requiere project_id")

    audience = brief.get("audience") or {}
    assumptions = brief.get("assumptions") or []
    if not isinstance(audience, dict) or not isinstance(assumptions, list):
        raise MissionValidationError("audience y assumptions deben tener forma válida")
    hypotheses: list[MissionHypothesis] = []
    audience_text = audience.get("hypothesis")
    if audience_text:
        hypotheses.append(
            MissionHypothesis(
                hypothesis_id="hyp-audience",
                text=str(audience_text),
                source_path="audience.hypothesis",
            )
        )
    for index, assumption in enumerate(assumptions):
        if not isinstance(assumption, dict) or not assumption.get("text"):
            raise MissionValidationError(f"assumptions[{index}] requiere text")
        hypotheses.append(
            MissionHypothesis(
                hypothesis_id=f"hyp-assumption-{index + 1}",
                text=str(assumption["text"]),
                source_path=f"assumptions[{index}]",
            )
        )
    if len(hypotheses) > limits.max_hypotheses:
        raise MissionBudgetExceeded("El brief excede max_hypotheses")

    raw_constraints = brief.get("user_constraints") or []
    if len(raw_constraints) > limits.max_constraints:
        raise MissionBudgetExceeded("El brief excede max_constraints")
    constraints: list[MissionConstraint] = []
    constraint_values: dict[str, str] = {}
    for index, item in enumerate(raw_constraints):
        if not isinstance(item, dict):
            raise MissionValidationError(f"user_constraints[{index}] debe ser objeto")
        if item.get("source", "user") != "user":
            continue
        key = item.get("key")
        if not isinstance(key, str) or not key.strip() or "value" not in item:
            raise MissionValidationError(
                f"user_constraints[{index}] requiere key y value"
            )
        try:
            value_json = _canonical_json(item["value"])
        except (TypeError, ValueError) as exc:
            raise MissionValidationError(
                f"user_constraints[{index}].value debe ser JSON serializable"
            ) from exc
        if key in constraint_values and constraint_values[key] != value_json:
            raise MissionValidationError(
                f"restricciones de usuario contradictorias: {key}"
            )
        constraint_values[key] = value_json
        constraints.append(
            MissionConstraint(
                key=key,
                value=item["value"],
                source_path=f"user_constraints[{index}]",
                source_text=str(item.get("text") or ""),
            )
        )

    raw_facts = brief.get("asset_facts") or []
    if len(raw_facts) > limits.max_asset_facts:
        raise MissionBudgetExceeded("El brief excede max_asset_facts")
    source_refs: list[dict[str, Any]] = [
        {"path": "purpose", "kind": "brief_field"},
        {"path": "main_message", "kind": "brief_field"},
    ]
    evidence_refs: list[str] = []
    for index, fact in enumerate(raw_facts):
        if not isinstance(fact, dict) or not fact.get("id"):
            raise MissionValidationError(f"asset_facts[{index}] requiere id")
        fact_ref = f"asset_facts[{index}]"
        evidence_refs.append(fact_ref)
        source_refs.append(
            {
                "path": fact_ref,
                "kind": str(fact.get("kind") or "unknown"),
                "fact_id": str(fact["id"]),
                "asset_id": str(fact.get("asset_id") or ""),
                "simulated": bool(fact.get("simulated", False)),
            }
        )

    duration_data = brief.get("duration") or {}
    target_duration = duration_data.get("target_sec")
    if target_duration is None:
        target_duration = 15.0
    try:
        total_duration = float(target_duration)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError(
            "duration.target_sec debe ser numérico"
        ) from exc
    if not math.isfinite(total_duration) or total_duration <= 0:
        raise MissionValidationError(
            "duration.target_sec debe ser finito y positivo"
        )
    if total_duration < _MIN_SHOT_SECONDS:
        raise MissionValidationError(
            f"duration.target_sec debe ser al menos {_MIN_SHOT_SECONDS:g} segundos"
        )
    shot_count = max(1, math.ceil(total_duration / _DEFAULT_SHOT_SECONDS))
    if shot_count > limits.max_shots:
        raise MissionBudgetExceeded(
            f"duration.target_sec requiere {shot_count} shots; "
            f"max_shots={limits.max_shots}"
        )

    selected_cost_cap = (
        cost_cap_usd
        if cost_cap_usd is not None
        else (brief.get("budget") or {}).get("cost_cap_usd", 0.0)
    )
    try:
        selected_cost_cap = float(selected_cost_cap)
    except (TypeError, ValueError) as exc:
        raise MissionValidationError("cost_cap_usd debe ser numérico") from exc
    if not math.isfinite(selected_cost_cap) or selected_cost_cap < 0:
        raise MissionValidationError(
            "cost_cap_usd debe ser finito y no negativo"
        )
    if selected_cost_cap > limits.max_cost_cap_usd:
        raise MissionBudgetExceeded("cost_cap_usd excede max_cost_cap_usd")

    stable_brief = _canonical_brief(brief)
    idempotency_material = _canonical_json(
        {
            "brief": stable_brief,
            "cost_cap_usd": selected_cost_cap,
            "limits": limits.to_dict(),
        }
    )
    key = hashlib.sha256(idempotency_material.encode("utf-8")).hexdigest()
    message = str(brief.get("main_message") or brief.get("purpose") or "").strip()
    if not message:
        raise MissionValidationError("El brief requiere main_message o purpose")
    titles = ("Apertura", "Desarrollo", "Cierre")
    task_ids = [f"task-{key[:12]}-{index + 1:02d}" for index in range(shot_count)]
    rendered_constraints = "; ".join(
        f"{item.key}={_canonical_json(item.value)}" for item in constraints
    )
    for index in range(shot_count):
        title = titles[index] if index < len(titles) else f"Desarrollo {index}"
        prompt = f"{title}: {message}"
        if rendered_constraints:
            prompt = f"{prompt}\nRestricciones explícitas: {rendered_constraints}"
        if len(prompt) > limits.max_prompt_chars:
            raise MissionBudgetExceeded("El prompt compilado excede max_prompt_chars")
    task_refs = (
        "purpose",
        "main_message",
        *(item.source_path for item in hypotheses),
        *(item.source_path for item in constraints),
        *evidence_refs,
    )
    tasks = tuple(
        MissionTask(
            task_id=task_ids[index],
            title=titles[index] if index < len(titles) else f"Desarrollo {index}",
            description=(
                f"{titles[index] if index < len(titles) else f'Desarrollo {index}'}: "
                f"{message}"
            ),
            duration_s=round(total_duration / shot_count, 3),
            depends_on=(task_ids[index - 1],) if index else (),
            source_refs=task_refs,
        )
        for index in range(shot_count)
    )
    topological_task_order(tasks)
    return MissionCompilation(
        schema=MISSION_SCHEMA,
        idempotency_key=key,
        hypotheses=tuple(hypotheses),
        constraints=tuple(constraints),
        source_refs=tuple(source_refs),
        tasks=tasks,
        cost_cap_usd=selected_cost_cap,
        limits=limits,
    )


__all__ = [
    "DEFAULT_LIMITS",
    "MISSION_SCHEMA",
    "MissionBudgetExceeded",
    "MissionCompilation",
    "MissionCompileError",
    "MissionConstraint",
    "MissionCycleError",
    "MissionHypothesis",
    "MissionLimits",
    "MissionTask",
    "MissionValidationError",
    "compile_mission",
    "topological_task_order",
]
