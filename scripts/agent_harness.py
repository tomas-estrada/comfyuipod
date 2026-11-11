#!/usr/bin/env python3
"""Launch an external coding agent with a durable, bounded run envelope.

This adapter enforces process wall time and records exit status. Tool access,
context accounting, and runtime-created subagents remain advisory unless the
selected CLI exposes hooks for them; see docs/agent-harness-contract.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-id", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--role", default="primary")
    parser.add_argument("--tools", default="")
    parser.add_argument("--max-iterations", type=int, default=12)
    parser.add_argument("--max-llm-calls", type=int, default=24)
    parser.add_argument("--max-context-chars", type=int, default=4000000)
    parser.add_argument("--max-wall-seconds", type=float, default=1800)
    parser.add_argument("--task", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("provide a command after --; use {prompt} where the task belongs")
    if (
        min(args.max_iterations, args.max_llm_calls, args.max_context_chars) < 1
        or args.max_wall_seconds <= 0
    ):
        parser.error("budgets must be positive")
    run_id = str(uuid.uuid4())
    task_hash = hashlib.sha256(args.task.encode("utf-8")).hexdigest()
    allowed = sorted({part.strip() for part in args.tools.split(",") if part.strip()})
    run_dir = Path(os.environ.get("AGENT_RUNS_DIR", ".agent-runs")) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    spec = {
        "schema": "comfyuipod.agent-run",
        "schema_version": 1,
        "run_id": run_id,
        "task_id": str(uuid.uuid4()),
        "runtime_id": args.runtime_id,
        "model_id": args.model_id,
        "task_sha256": task_hash,
        "allowed_tools": allowed,
        "max_iterations": args.max_iterations,
        "max_llm_calls": args.max_llm_calls,
        "max_context_chars": args.max_context_chars,
        "max_wall_seconds": args.max_wall_seconds,
        "role": args.role,
        "created_at": now(),
        "enforcement": {
            "process_wall_time": "enforced",
            "tool_allowlist": "advisory unless runtime hook is available",
            "llm_call_and_context_budgets": "advisory unless runtime hook is available",
            "runtime_subagents": "must use this adapter; otherwise untracked",
        },
    }
    record = {"spec": spec, "status": "running", "started_at": now(), "checkpoints": []}
    record_path = run_dir / "run.json"

    def save() -> None:
        temporary = record_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temporary.replace(record_path)

    save()
    prompt = (
        "Repository agent harness contract (run " + run_id + "):\n"
        + json.dumps(spec, ensure_ascii=False, sort_keys=True)
        + "\nUse only the declared tools and budgets where your runtime supports enforcement. "
        "Do not spawn an agent outside this adapter; create a child run with an intersected "
        "tool set and remaining budgets. Persist decisions, evidence and next steps outside "
        "your context before compaction. Finish with findings, files changed, checks, "
        "and limits.\n\n"
        + args.task
    )
    command = [prompt if part == "{prompt}" else part for part in command]
    record["checkpoints"].append({"at": now(), "stage": "process_started"})
    save()
    started = time.monotonic()
    try:
        completed = subprocess.run(command, check=False, timeout=args.max_wall_seconds)
        record["status"] = "completed" if completed.returncode == 0 else "failed"
        record["exit_code"] = completed.returncode
    except subprocess.TimeoutExpired:
        record["status"] = "wall_budget_exhausted"
        record["exit_code"] = None
    except KeyboardInterrupt:
        record["status"] = "interrupted"
        record["exit_code"] = None
    finally:
        record["finished_at"] = now()
        record["wall_seconds"] = round(time.monotonic() - started, 3)
        record["checkpoints"].append({"at": record["finished_at"], "stage": "process_finished"})
        save()
    print(
        f"Agent run {run_id}: {record['status']} ({record.get('exit_code')}); "
        f"evidence: {record_path}"
    )
    if record.get("exit_code") is not None:
        return int(record["exit_code"])
    if record["status"] == "wall_budget_exhausted":
        return 124
    return 130 if record["status"] == "interrupted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
