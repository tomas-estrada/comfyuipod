from __future__ import annotations

import pytest
from pydantic import BaseModel

from comfy_studio.agent.harness import (
    AgentHarnessError,
    AgentRunRecord,
    AgentRunSpec,
    AgentRunStore,
)
from comfy_studio.agent.tools import ToolBelt, ToolContext, ToolExecutionError, ToolSpec


def make_spec(**overrides) -> AgentRunSpec:
    values = {
        "task": "Crear un borrador",
        "runtime_id": "test.runtime",
        "model_id": "test-model",
        "allowed_tools": ["read", "write"],
    }
    values.update(overrides)
    return AgentRunSpec.create(**values)


def test_contract_bounds_context_to_system_and_latest_user_block() -> None:
    spec = make_spec(max_context_messages=4, max_context_chars=3000)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "answer"},
    ]
    assert spec.bound_messages(messages) == [messages[0], *messages[-2:]]


def test_contract_fails_closed_when_essential_context_is_over_budget() -> None:
    spec = make_spec(max_context_chars=1024)
    with pytest.raises(AgentHarnessError):
        spec.bound_messages([{"role": "system", "content": "x" * 1200}])


def test_child_contract_cannot_widen_tools_or_budgets() -> None:
    parent = make_spec(max_iterations=5, max_llm_calls=8)
    child = parent.child(
        task="revisar",
        role="reviewer",
        allowed_tools=["read", "delete"],
        max_iterations=20,
        remaining_llm_calls=3,
    )
    assert child.allowed_tools == ("read",)
    assert child.max_iterations == 5
    assert child.max_llm_calls == 3
    assert child.parent_run_id == parent.run_id
    with pytest.raises(AgentHarnessError):
        parent.child(
            task="sin presupuesto",
            role="reviewer",
            allowed_tools=["read"],
            max_iterations=1,
            remaining_llm_calls=0,
        )


@pytest.mark.anyio
async def test_toolbelt_checks_allowlist_at_dispatch() -> None:
    async def handler(_ctx, _args):
        return {"ok": True}

    belt = ToolBelt()
    belt.register(ToolSpec(name="read", description="Read", args_model=BaseModel, handler=handler))
    ctx = ToolContext(project_id="p", store=None, comfyui_client=None, broker=None, config=None,
                      allowed_tools=frozenset())
    with pytest.raises(ToolExecutionError, match="no permitida por el harness"):
        await belt.execute("read", {}, ctx)


def test_run_record_persists_secret_free_evidence(studio_data_dir) -> None:
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="harness")
    run_store = AgentRunStore(store, project["id"])
    record = AgentRunRecord(make_spec(task="token=never-persist-this"))
    record.checkpoint("started", {"phase": "planning"})
    record.record_tool("read", ok=True, iteration=1)
    record.finish("completed", completion_evidence={"result_sha256": "a" * 64})
    run_store.save(record)
    restored = run_store.load(record.spec.run_id)
    assert restored is not None
    assert restored["status"] == "completed"
    assert restored["tool_evidence"][0]["name"] == "read"
    assert "never-persist-this" not in str(restored)
