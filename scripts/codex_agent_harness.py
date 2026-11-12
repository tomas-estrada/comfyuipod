#!/usr/bin/env python3
"""Codex lifecycle-hook adapter for the shared ComfyUIPod agent contract.

Codex hooks can deny local tool calls, but model-request hooks are diagnostic
and hosted tools bypass the local hook path. The run record labels those limits
as advisory instead of claiming enforcement.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "schema": "comfyuipod.agent-runtime",
    "schema_version": 1,
    "runtime_id": "codex",
    "allowed_tools": ["Agent", "Bash", "apply_patch"],
    "max_iterations": 12,
    "max_llm_calls": 24,
    "max_tool_calls": 200,
    "max_context_chars": 4_000_000,
    "max_wall_seconds": 1800,
}
TERMINAL = {
    "completed",
    "failed",
    "cancelled",
    "max_iterations",
    "budget_exhausted",
    "wall_budget_exhausted",
    "interrupted",
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _repo_root(cwd: str) -> Path:
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "docs/agent-harness-contract.md").is_file() and (
            candidate / ".git"
        ).exists():
            return candidate
    raise ValueError("Codex harness could not find the ComfyUIPod repository root")


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+") as lock:
        os.chmod(path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _checkpoint(record: dict[str, Any], stage: str, evidence: dict[str, Any] | None = None) -> None:
    record.setdefault("checkpoints", []).append(
        {"at": now(), "stage": stage, "evidence": evidence or {}}
    )
    record["checkpoints"] = record["checkpoints"][-100:]
    record["updated_at"] = now()


def _ensure_checkpoint(path: Path, record: dict[str, Any]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        "# Agent checkpoint\n\n"
        f"- Run: {record['spec']['run_id']}\n"
        f"- Task: {record['spec']['task_id']}\n"
        "- Status: started\n\n"
        "## Decisions\n\n## Verified evidence\n\n## Blockers\n\n## Next action\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _finish(record: dict[str, Any], status: str, evidence: dict[str, Any] | None = None) -> None:
    if record.get("status") in TERMINAL:
        return
    record["status"] = status
    record["finished_at"] = now()
    record["updated_at"] = record["finished_at"]
    record["completion_evidence"] = evidence or {}


def _new_record(
    *,
    config: dict[str, Any],
    task: str,
    model: str,
    role: str,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = now()
    run_id = str(uuid.uuid4())
    return {
        "schema": "comfyuipod.agent-run",
        "schema_version": 1,
        "spec": {
            "run_id": run_id,
            "task_id": parent["spec"]["task_id"] if parent else str(uuid.uuid4()),
            "runtime_id": config["runtime_id"],
            "model_id": model or "unknown",
            "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
            "allowed_tools": sorted(config["allowed_tools"]),
            "max_iterations": int(config["max_iterations"]),
            "max_llm_calls": int(config["max_llm_calls"]),
            "max_context_chars": int(config["max_context_chars"]),
            "max_wall_seconds": float(config["max_wall_seconds"]),
            "parent_run_id": parent["spec"]["run_id"] if parent else None,
            "role": role,
            "created_at": created,
            "task_sha256_source": (
                "subagent role label only; Codex hook does not expose child task text"
                if parent
                else "awaiting UserPromptSubmit prompt"
            ),
        },
        "status": "running",
        "started_at": created,
        "updated_at": created,
        "finished_at": None,
        "provider": "codex",
        "resolved_model_id": model or None,
        "iterations_used": 0,
        "llm_calls_used": 0,
        "tool_evidence": [],
        "checkpoints": [],
        "completion_evidence": {},
        "error_code": None,
        "enforcement": {
            "local_tool_allowlist": (
                "enforced by synchronous PreToolUse hook when hooks are trusted"
            ),
            "tool_and_wall_budgets": "enforced at local tool dispatch boundaries",
            "iteration_budget": ("advisory; Codex exposes no blocking loop-iteration hook"),
            "child_context_and_tool_scope": (
                "injected and checked through SubagentStart/PreToolUse hooks"
            ),
            "llm_call_budget": "advisory; Codex model-request hooks cannot block requests",
            "context_chars": "advisory; Codex owns context and automatic compaction",
            "hosted_tools": "outside local hook coverage",
        },
    }


def _tool_allowed(name: str, allowlist: list[str]) -> bool:
    return name in allowlist or ("mcp__*" in allowlist and name.startswith("mcp__"))


def _context(record: dict[str, Any], run_path: Path, group: dict[str, Any]) -> str:
    spec = record["spec"]
    return (
        "ComfyUIPod harness contract v1 is active. "
        f"Run {spec['run_id']} (parent {spec.get('parent_run_id') or 'none'}); "
        f"allowed local tools: {', '.join(spec['allowed_tools'])}; "
        "shared tool calls remaining: "
        f"{max(0, int(group['max_tool_calls']) - int(group['tool_calls']))}. "
        f"Checkpoint decisions/evidence in {run_path.parent / 'checkpoint.md'} before compaction. "
        "Do not start an agent through an unregistered runtime. Hosted tools and model-call limits "
        "are not intercepted by Codex local hooks and remain advisory."
    )


def handle(event: dict[str, Any], *, root: Path, runs_dir: Path) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    config_path = root / ".codex/agent-harness.json"
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Codex harness configuration is unreadable") from exc
        if (
            not isinstance(loaded, dict)
            or loaded.get("schema") != "comfyuipod.agent-runtime"
            or loaded.get("schema_version") != 1
        ):
            raise ValueError("Codex harness configuration has an invalid schema")
        config.update(loaded)
    if (
        not isinstance(config.get("allowed_tools"), list)
        or any(not isinstance(tool, str) or not tool for tool in config["allowed_tools"])
        or len(config["allowed_tools"]) != len(set(config["allowed_tools"]))
        or any(
            int(config[key]) < 1
            for key in ("max_iterations", "max_llm_calls", "max_tool_calls", "max_context_chars")
        )
        or not math.isfinite(float(config["max_wall_seconds"]))
        or float(config["max_wall_seconds"]) <= 0
    ):
        raise ValueError("Codex harness configuration contains invalid limits or tool names")
    sessions_path = runs_dir / ".codex-index.json"
    lock_path = runs_dir / ".codex-index.lock"
    event_name = str(event.get("hook_event_name") or "")
    session_id = str(event.get("session_id") or "")
    turn_id = str(event.get("turn_id") or "")
    agent_id = str(event.get("agent_id") or "")
    runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(runs_dir, 0o700)

    with _locked(lock_path):
        index = _load_json(sessions_path, {"sessions": {}, "turns": {}, "agents": {}, "groups": {}})
        run_id = (
            index["turns"].get(turn_id)
            or index["agents"].get(agent_id)
            or index["sessions"].get(session_id)
        )
        run_path = runs_dir / run_id / "run.json" if run_id else None
        record = _load_json(run_path, None) if run_path else None

        if event_name == "SessionStart":
            if event.get("source") == "compact" and record:
                _checkpoint(record, "context_compacted")
            elif not record or record.get("status") in TERMINAL:
                record = _new_record(
                    config=config,
                    task="Codex session awaiting task",
                    model=str(event.get("model") or ""),
                    role="primary",
                )
                run_id = record["spec"]["run_id"]
                run_path = runs_dir / run_id / "run.json"
                index["sessions"][session_id] = run_id
                index["groups"][record["spec"]["task_id"]] = {
                    "started_at": now(),
                    "tool_calls": 0,
                    "max_tool_calls": int(config["max_tool_calls"]),
                }
                _checkpoint(record, "session_started", {"source": event.get("source", "unknown")})
            group = index["groups"][record["spec"]["task_id"]]
            _atomic_json(run_path, record)
            _ensure_checkpoint(run_path.parent / "checkpoint.md", record)
            _atomic_json(sessions_path, index)
            return {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": _context(record, run_path, group),
                }
            }

        if event_name == "UserPromptSubmit":
            if not record or record.get("status") in TERMINAL:
                record = _new_record(
                    config=config,
                    task="Codex session task",
                    model=str(event.get("model") or ""),
                    role="primary",
                )
                run_id = record["spec"]["run_id"]
                run_path = runs_dir / run_id / "run.json"
                index["sessions"][session_id] = run_id
                index["groups"][record["spec"]["task_id"]] = {
                    "started_at": now(),
                    "tool_calls": 0,
                    "max_tool_calls": int(config["max_tool_calls"]),
                }
            prompt = str(event.get("prompt") or "")
            record["spec"]["task_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            record["spec"]["task_sha256_source"] = (
                "UserPromptSubmit prompt hashed; raw prompt omitted"
            )
            _checkpoint(
                record, "task_submitted", {"prompt_chars": len(prompt), "turn_id": turn_id or None}
            )
            group = index["groups"][record["spec"]["task_id"]]
            _atomic_json(run_path, record)
            _ensure_checkpoint(run_path.parent / "checkpoint.md", record)
            _atomic_json(sessions_path, index)
            return {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": _context(record, run_path, group),
                }
            }

        if event_name == "SubagentStart":
            parent_id = index["sessions"].get(session_id)
            parent_path = runs_dir / parent_id / "run.json" if parent_id else None
            parent = _load_json(parent_path, None) if parent_path else None
            if not parent:
                return {
                    "systemMessage": (
                        "ComfyUIPod harness: subagent started without a registered parent "
                        "run; it is untracked."
                    )
                }
            child_cfg = dict(config)
            child_cfg["allowed_tools"] = [
                tool for tool in parent["spec"]["allowed_tools"] if tool != "Agent"
            ]
            role = str(event.get("agent_type") or "subagent")
            record = _new_record(
                config=child_cfg,
                task=f"Codex subagent {role}",
                model=str(event.get("model") or parent["spec"]["model_id"]),
                role=role,
                parent=parent,
            )
            run_id = record["spec"]["run_id"]
            run_path = runs_dir / run_id / "run.json"
            index["agents"][agent_id] = run_id
            if turn_id:
                index["turns"][turn_id] = run_id
            _checkpoint(record, "subagent_started", {"agent_type": role})
            group = index["groups"][parent["spec"]["task_id"]]
            _atomic_json(run_path, record)
            (run_path.parent / "checkpoint.md").write_text(
                "# Agent checkpoint\n\n- Status: started\n\n## Decisions\n\n"
                "## Verified evidence\n\n## Blockers\n\n## Next action\n",
                encoding="utf-8",
            )
            os.chmod(run_path.parent / "checkpoint.md", 0o600)
            _atomic_json(sessions_path, index)
            return {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": _context(record, run_path, group),
                }
            }

        if not record or not run_path:
            if event_name == "PreToolUse":
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "ComfyUIPod agent run contract missing; start the session with "
                            "trusted repository hooks."
                        ),
                    }
                }
            return {}

        task_id = record["spec"]["task_id"]
        group = index["groups"].get(
            task_id,
            {
                "started_at": record["started_at"],
                "tool_calls": 0,
                "max_tool_calls": int(config["max_tool_calls"]),
            },
        )
        if event_name == "PreToolUse":
            tool_name = str(event.get("tool_name") or "")
            started = datetime.fromisoformat(group["started_at"])
            wall_seconds = (datetime.now(UTC) - started).total_seconds()
            reason = None
            if record["status"] != "running":
                reason = "agent run is already terminal"
            elif not _tool_allowed(tool_name, record["spec"]["allowed_tools"]):
                reason = f"tool {tool_name!r} is outside this run's allowlist"
            elif wall_seconds >= float(record["spec"]["max_wall_seconds"]):
                reason = "shared wall-time budget exhausted"
                _finish(record, "wall_budget_exhausted", {"wall_seconds": round(wall_seconds, 3)})
            elif int(group["tool_calls"]) >= int(group["max_tool_calls"]):
                reason = "shared tool-call budget exhausted"
                _finish(record, "budget_exhausted")
            if reason:
                record.setdefault("tool_evidence", []).append(
                    {
                        "at": now(),
                        "name": tool_name,
                        "allowed": False,
                        "executed": False,
                        "reason": reason,
                    }
                )
                _atomic_json(run_path, record)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": f"ComfyUIPod harness: {reason}.",
                    }
                }
            group["tool_calls"] = int(group["tool_calls"]) + 1
            index["groups"][task_id] = group
            record.setdefault("tool_evidence", []).append(
                {
                    "at": now(),
                    "tool_use_id": event.get("tool_use_id"),
                    "name": tool_name,
                    "allowed": True,
                    "executed": True,
                    "ok": None,
                    "iteration": record["llm_calls_used"],
                }
            )
            record["tool_evidence"] = record["tool_evidence"][-200:]
            _atomic_json(run_path, record)
            _atomic_json(sessions_path, index)
            return {}

        if event_name == "PostToolUse":
            _checkpoint(
                record,
                "tool_finished",
                {
                    "tool": str(event.get("tool_name") or "unknown"),
                    "result_received": "tool_response" in event,
                },
            )
        elif event_name == "PreModelRequest":
            record["llm_calls_used"] = int(record.get("llm_calls_used", 0)) + 1
            if record["llm_calls_used"] > int(record["spec"]["max_llm_calls"]):
                _checkpoint(
                    record,
                    "model_call_budget_exceeded_advisory",
                    {"llm_calls": record["llm_calls_used"]},
                )
        elif event_name == "PostModelRequest":
            usage = event.get("token_usage")
            evidence = {"status": event.get("status")}
            if isinstance(usage, dict):
                evidence["token_usage"] = {
                    key: int(value)
                    for key, value in usage.items()
                    if isinstance(value, (int, float))
                }
            _checkpoint(record, "model_request_finished", evidence)
        elif event_name in {"PreCompact", "PostCompact"}:
            phase = "before" if event_name == "PreCompact" else "after"
            _checkpoint(record, "context_compaction_" + phase, {"trigger": event.get("trigger")})
            _atomic_json(run_path, record)
            group = index["groups"].get(
                task_id, {"tool_calls": 0, "max_tool_calls": int(config["max_tool_calls"])}
            )
            return {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": _context(record, run_path, group),
                }
            }
        elif event_name in {"Stop", "SubagentStop"}:
            _finish(
                record,
                "completed",
                {"assistant_message_chars": len(str(event.get("last_assistant_message") or ""))},
            )
            if event_name == "SubagentStop":
                _checkpoint(record, "subagent_stopped")
        elif event_name == "Interrupt":
            _finish(record, "interrupted", {"turn_id": turn_id or None})
        elif event_name == "SessionEnd":
            _finish(record, "interrupted", {"reason": event.get("reason", "session_ended")})
        _atomic_json(run_path, record)
        return {}


def main() -> int:
    event: Any = {}
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input must be a JSON object")
        root = _repo_root(str(event.get("cwd") or os.getcwd()))
        runs_dir = Path(os.environ.get("AGENT_RUNS_DIR", str(root / ".agent-runs"))).resolve()
        output = handle(event, root=root, runs_dir=runs_dir)
        if output:
            print(json.dumps(output, ensure_ascii=False))
        return 0
    except Exception as exc:  # Hooks must deny dispatch if their guard fails.
        event_name = event.get("hook_event_name") if isinstance(event, dict) else None
        if event_name == "PreToolUse":
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": (
                                f"ComfyUIPod harness error: {type(exc).__name__}"
                            ),
                        }
                    }
                )
            )
            return 0
        if event_name == "SessionStart":
            print(
                json.dumps(
                    {
                        "continue": False,
                        "stopReason": f"ComfyUIPod harness failed: {type(exc).__name__}",
                    }
                )
            )
            return 0
        if event_name == "UserPromptSubmit":
            print(
                json.dumps(
                    {
                        "decision": "block",
                        "reason": f"ComfyUIPod harness failed: {type(exc).__name__}",
                    }
                )
            )
            return 0
        print(f"ComfyUIPod Codex harness failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
