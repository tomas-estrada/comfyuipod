"""Codex hook adapter contract and dispatch enforcement fixtures."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts import codex_agent_harness as harness  # noqa: E402


def _run_record(runs_dir: Path, run_id: str) -> dict:
    return json.loads((runs_dir / run_id / "run.json").read_text(encoding="utf-8"))


def test_codex_hook_configuration_registers_lifecycle_boundaries() -> None:
    root = Path(__file__).resolve().parents[2]
    hooks = json.loads((root / ".codex/hooks.json").read_text(encoding="utf-8"))["hooks"]
    assert {"SessionStart", "UserPromptSubmit", "PreToolUse", "SubagentStart", "Stop"} <= set(hooks)
    assert (
        "mcp__*"
        not in json.loads((root / ".codex/agent-harness.json").read_text(encoding="utf-8"))[
            "allowed_tools"
        ]
    )


def test_codex_hook_lifecycle_records_parent_and_bounded_child(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    runs_dir = tmp_path / ".agent-runs"
    start = harness.handle(
        {
            "hook_event_name": "SessionStart",
            "session_id": "root-session",
            "source": "startup",
            "cwd": str(root),
            "model": "test-model",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert start["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    index = json.loads((runs_dir / ".codex-index.json").read_text(encoding="utf-8"))
    root_id = index["sessions"]["root-session"]

    prompt = "Inspect code without persisting prompt contents"
    harness.handle(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "root-session",
            "turn_id": "root-turn-1",
            "prompt": prompt,
        },
        root=root,
        runs_dir=runs_dir,
    )
    root_record = _run_record(runs_dir, root_id)
    assert root_record["spec"]["task_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    assert prompt not in (runs_dir / root_id / "run.json").read_text(encoding="utf-8")

    allowed = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "root-session",
            "turn_id": "root-turn-1",
            "tool_use_id": "tool-1",
            "tool_name": "Bash",
        },
        root=root,
        runs_dir=runs_dir,
    )
    denied = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "root-session",
            "turn_id": "root-turn-1",
            "tool_use_id": "tool-2",
            "tool_name": "unlisted_tool",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert allowed == {}
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    child_context = harness.handle(
        {
            "hook_event_name": "SubagentStart",
            "session_id": "root-session",
            "turn_id": "child-turn-1",
            "agent_id": "child-agent-1",
            "agent_type": "implementer",
            "model": "child-model",
        },
        root=root,
        runs_dir=runs_dir,
    )
    index = json.loads((runs_dir / ".codex-index.json").read_text(encoding="utf-8"))
    child_id = index["agents"]["child-agent-1"]
    child = _run_record(runs_dir, child_id)
    assert child["spec"]["parent_run_id"] == root_id
    assert "role label only" in child["spec"]["task_sha256_source"]
    assert child["spec"]["task_id"] == root_record["spec"]["task_id"]
    assert "Agent" not in child["spec"]["allowed_tools"]
    assert "shared tool calls remaining" in child_context["hookSpecificOutput"]["additionalContext"]

    denied_child_delegate = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "root-session",
            "turn_id": "child-turn-1",
            "tool_use_id": "tool-3",
            "tool_name": "Agent",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert denied_child_delegate["hookSpecificOutput"]["permissionDecision"] == "deny"

    harness.handle(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "root-session",
            "turn_id": "child-turn-1",
            "agent_id": "child-agent-1",
            "last_assistant_message": "finished",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert _run_record(runs_dir, child_id)["status"] == "completed"

    harness.handle(
        {
            "hook_event_name": "Stop",
            "session_id": "root-session",
            "turn_id": "root-turn-1",
            "last_assistant_message": "finished",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert _run_record(runs_dir, root_id)["status"] == "completed"


def test_codex_hook_fails_closed_without_a_registered_session(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    result = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "unknown-session",
            "tool_name": "Bash",
        },
        root=root,
        runs_dir=tmp_path / ".agent-runs",
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_hook_starts_a_new_run_for_a_followup_turn(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    runs_dir = tmp_path / ".agent-runs"
    harness.handle(
        {
            "hook_event_name": "SessionStart",
            "session_id": "session",
            "source": "startup",
            "cwd": str(root),
        },
        root=root,
        runs_dir=runs_dir,
    )
    index = json.loads((runs_dir / ".codex-index.json").read_text(encoding="utf-8"))
    first_id = index["sessions"]["session"]
    harness.handle(
        {"hook_event_name": "Stop", "session_id": "session", "last_assistant_message": "done"},
        root=root,
        runs_dir=runs_dir,
    )
    harness.handle(
        {"hook_event_name": "UserPromptSubmit", "session_id": "session", "prompt": "next task"},
        root=root,
        runs_dir=runs_dir,
    )
    index = json.loads((runs_dir / ".codex-index.json").read_text(encoding="utf-8"))
    assert index["sessions"]["session"] != first_id
    assert _run_record(runs_dir, index["sessions"]["session"])["status"] == "running"


def test_codex_hook_enforces_a_shared_tool_budget(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/agent-harness-contract.md").write_text("contract", encoding="utf-8")
    (root / ".git").mkdir()
    config_path = root / ".codex/agent-harness.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "schema": "comfyuipod.agent-runtime",
                "schema_version": 1,
                "runtime_id": "codex",
                "allowed_tools": ["Bash"],
                "max_tool_calls": 1,
            }
        ),
        encoding="utf-8",
    )
    runs_dir = tmp_path / ".agent-runs"
    harness.handle(
        {
            "hook_event_name": "SessionStart",
            "session_id": "bounded-session",
            "source": "startup",
        },
        root=root,
        runs_dir=runs_dir,
    )
    allowed = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "bounded-session",
            "tool_use_id": "one",
            "tool_name": "Bash",
        },
        root=root,
        runs_dir=runs_dir,
    )
    denied = harness.handle(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "bounded-session",
            "tool_use_id": "two",
            "tool_name": "Bash",
        },
        root=root,
        runs_dir=runs_dir,
    )
    assert allowed == {}
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    index = json.loads((runs_dir / ".codex-index.json").read_text(encoding="utf-8"))
    record = _run_record(runs_dir, index["sessions"]["bounded-session"])
    assert record["status"] == "budget_exhausted"


def test_codex_cli_session_start_fails_closed_on_invalid_configuration(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    bad_root = tmp_path / "bad-repo"
    (bad_root / "docs").mkdir(parents=True)
    (bad_root / "docs/agent-harness-contract.md").write_text("contract", encoding="utf-8")
    (bad_root / ".git").mkdir()
    (bad_root / ".codex").mkdir()
    (bad_root / ".codex/agent-harness.json").write_text("invalid", encoding="utf-8")
    event = {
        "hook_event_name": "SessionStart",
        "session_id": "bad-session",
        "source": "startup",
        "cwd": str(bad_root),
    }
    process = subprocess.run(
        [sys.executable, str(root / "scripts/codex_agent_harness.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0
    assert json.loads(process.stdout)["continue"] is False
