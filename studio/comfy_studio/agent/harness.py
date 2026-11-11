"""Shared run contract and durable evidence for ComfyUIPod agents.

The Studio runtime can enforce this contract in process. External CLIs consume
the same shape through their launch adapters, but capabilities they do not
expose remain advisory and must not be reported as enforced.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

RunStatus = Literal[
    "running",
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "budget_exhausted",
    "wall_budget_exhausted",
    "interrupted",
]


class AgentHarnessError(ValueError):
    """A run contract or enforcement boundary was invalid."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _message_text_size(message: dict[str, Any]) -> int:
    try:
        return len(json.dumps(message, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(message))


@dataclass(frozen=True, slots=True)
class AgentRunSpec:
    """Immutable execution contract shared by Studio and CLI adapters."""

    run_id: str
    task_id: str
    runtime_id: str
    model_id: str
    task_sha256: str
    allowed_tools: tuple[str, ...]
    max_iterations: int = 12
    max_llm_calls: int = 24
    max_context_messages: int = 60
    max_context_chars: int = 4_000_000
    max_wall_seconds: float = 1800.0
    parent_run_id: str | None = None
    role: str = "primary"
    created_at: str = field(default_factory=_utc_now)

    @classmethod
    def create(
        cls,
        *,
        task: str,
        runtime_id: str,
        model_id: str,
        allowed_tools: list[str] | tuple[str, ...],
        max_iterations: int = 12,
        max_llm_calls: int = 24,
        max_context_messages: int = 60,
        max_context_chars: int = 4_000_000,
        max_wall_seconds: float = 1800.0,
        parent_run_id: str | None = None,
        role: str = "primary",
        task_id: str | None = None,
    ) -> AgentRunSpec:
        task_text = str(task or "").strip()
        spec = cls(
            run_id=str(uuid.uuid4()),
            task_id=task_id or str(uuid.uuid4()),
            runtime_id=runtime_id,
            model_id=model_id,
            task_sha256=hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
            allowed_tools=tuple(sorted(set(map(str, allowed_tools)))),
            max_iterations=max_iterations,
            max_llm_calls=max_llm_calls,
            max_context_messages=max_context_messages,
            max_context_chars=max_context_chars,
            max_wall_seconds=max_wall_seconds,
            parent_run_id=parent_run_id,
            role=role,
        )
        spec.validate()
        return spec

    def validate(self) -> None:
        for field_name in ("run_id", "task_id", "runtime_id", "model_id", "role"):
            if not str(getattr(self, field_name)).strip():
                raise AgentHarnessError(f"{field_name} no puede estar vacío")
        if len(self.task_sha256) != 64 or any(
            ch not in "0123456789abcdef" for ch in self.task_sha256
        ):
            raise AgentHarnessError("task_sha256 debe ser SHA-256 hexadecimal")
        if self.max_iterations < 1 or self.max_llm_calls < 1:
            raise AgentHarnessError(
                "los presupuestos de iteraciones y llamadas deben ser positivos"
            )
        if self.max_context_messages < 2 or self.max_context_chars < 1024:
            raise AgentHarnessError("el presupuesto de contexto es demasiado pequeño")
        if not math.isfinite(self.max_wall_seconds) or self.max_wall_seconds <= 0:
            raise AgentHarnessError("max_wall_seconds debe ser finito y positivo")
        if len(self.allowed_tools) != len(set(self.allowed_tools)):
            raise AgentHarnessError("allowed_tools contiene duplicados")

    def bound_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the system message and newest history within declared limits."""
        if len(messages) <= 1:
            bounded = list(messages)
        else:
            system = messages[:1]
            bounded = system + list(messages[1:])
        while (
            (
                len(bounded) > self.max_context_messages
                or sum(map(_message_text_size, bounded)) > self.max_context_chars
            )
            and len(bounded) > 2
        ):
            next_user = next(
                (
                    index
                    for index, message in enumerate(bounded[2:], start=2)
                    if message.get("role") == "user"
                ),
                None,
            )
            if next_user is None:
                break
            del bounded[1:next_user]
        if (
            len(bounded) > self.max_context_messages
            or sum(map(_message_text_size, bounded)) > self.max_context_chars
        ):
            raise AgentHarnessError("el contexto esencial excede max_context_chars")
        return bounded

    def child(
        self,
        *,
        task: str,
        role: str,
        allowed_tools: list[str] | tuple[str, ...],
        max_iterations: int,
        remaining_llm_calls: int,
        model_id: str | None = None,
    ) -> AgentRunSpec:
        """Create a child contract that cannot widen the parent's tool/budget scope."""
        allowed = set(allowed_tools) & set(self.allowed_tools)
        if not allowed:
            raise AgentHarnessError("el subagente no comparte herramientas permitidas con su padre")
        if remaining_llm_calls < 1:
            raise AgentHarnessError("el run padre agotó el presupuesto de llamadas LLM")
        return AgentRunSpec.create(
            task=task,
            runtime_id=self.runtime_id,
            model_id=model_id or self.model_id,
            allowed_tools=tuple(allowed),
            max_iterations=min(max_iterations, self.max_iterations),
            max_llm_calls=min(remaining_llm_calls, self.max_llm_calls),
            max_context_messages=self.max_context_messages,
            max_context_chars=self.max_context_chars,
            max_wall_seconds=self.max_wall_seconds,
            parent_run_id=self.run_id,
            role=role,
            task_id=self.task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentRunRecord:
    """Durable, secret-free status and evidence for one agent run."""

    spec: AgentRunSpec
    status: RunStatus = "running"
    started_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    finished_at: str | None = None
    provider: str | None = None
    resolved_model_id: str | None = None
    iterations_used: int = 0
    llm_calls_used: int = 0
    tool_evidence: list[dict[str, Any]] = field(default_factory=list)
    checkpoints: list[dict[str, Any]] = field(default_factory=list)
    completion_evidence: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None

    def checkpoint(self, stage: str, evidence: dict[str, Any] | None = None) -> None:
        if self.status != "running":
            raise AgentHarnessError("no se puede actualizar un run terminal")
        self.checkpoints.append(
            {"at": _utc_now(), "stage": stage, "evidence": dict(evidence or {})}
        )
        self.checkpoints = self.checkpoints[-100:]
        self.updated_at = _utc_now()

    def record_tool(self, name: str, *, ok: bool, iteration: int) -> None:
        self.tool_evidence.append(
            {
                "at": _utc_now(),
                "name": name,
                "allowed": name in self.spec.allowed_tools,
                "executed": name in self.spec.allowed_tools,
                "ok": bool(ok),
                "iteration": int(iteration),
            }
        )
        self.tool_evidence = self.tool_evidence[-200:]
        self.updated_at = _utc_now()

    def finish(
        self,
        status: RunStatus,
        *,
        completion_evidence: dict[str, Any] | None = None,
        provider: str | None = None,
        resolved_model_id: str | None = None,
        iterations_used: int = 0,
        llm_calls_used: int = 0,
        error_code: str | None = None,
    ) -> None:
        if self.status != "running":
            raise AgentHarnessError("el run ya es terminal")
        if status == "running":
            raise AgentHarnessError("finish requiere un estado terminal")
        self.status = status
        self.finished_at = _utc_now()
        self.updated_at = self.finished_at
        self.provider = provider
        self.resolved_model_id = resolved_model_id
        self.iterations_used = max(0, int(iterations_used))
        self.llm_calls_used = max(0, int(llm_calls_used))
        self.completion_evidence = dict(completion_evidence or {})
        self.error_code = error_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "comfyuipod.agent-run",
            "schema_version": 1,
            "spec": self.spec.to_dict(),
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "provider": self.provider,
            "resolved_model_id": self.resolved_model_id,
            "iterations_used": self.iterations_used,
            "llm_calls_used": self.llm_calls_used,
            "tool_evidence": list(self.tool_evidence),
            "checkpoints": list(self.checkpoints),
            "completion_evidence": dict(self.completion_evidence),
            "error_code": self.error_code,
        }


class AgentRunStore:
    """Persist run records through a ProjectStore-compatible metadata interface."""

    def __init__(self, store: Any, project_id: str) -> None:
        self._store = store
        self._project_id = project_id

    @staticmethod
    def _key(run_id: str) -> str:
        return f"agent_run:{run_id}"

    def save(self, record: AgentRunRecord) -> None:
        self._store.set_meta(self._project_id, self._key(record.spec.run_id), record.to_dict())

    def load(self, run_id: str) -> dict[str, Any] | None:
        value = self._store.get_meta(self._project_id, self._key(run_id))
        if not isinstance(value, dict) or value.get("schema") != "comfyuipod.agent-run":
            return None
        return value
