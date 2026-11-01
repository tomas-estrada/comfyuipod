"""Tests de las mejoras del loop M3-B: ejecución paralela de herramientas,
planner (update_plan + inyección en system), presupuesto de llamadas LLM y
delegación de roles con tool-belt restringido (tareas 9-13 del plan)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.agent.llm import OpenAICompatibleLLMClient
from comfy_studio.agent.loop import StudioAgent
from comfy_studio.agent.tools import (
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
    create_restricted_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.llm import StubLLMClient, StubLLMstep, StubToolCallSpec


@pytest.fixture()
def env(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
):
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Loop M3-B")
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
    )
    return store, broker, project, stub_llm, agent


@pytest.mark.anyio
async def test_parallel_safe_tools_execute_and_emit_in_original_order(env) -> None:
    store, _broker, project, stub_llm, agent = env
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Lanzo tres herramientas a la vez.",
            tool_calls=[
                StubToolCallSpec(name="search_assets", arguments={"query": "clip"}),
                StubToolCallSpec(name="list_templates", arguments={}),
                StubToolCallSpec(
                    name="update_plan",
                    arguments={"title": "Plan paralelo", "steps": [{"text": "paso 1"}]},
                ),
            ],
        )
    )
    stub_llm.enqueue_step(StubLLMstep(content="Todo listo."))

    result = await agent.run_turn(project_id=project["id"], user_message="haz las tres")

    tool_results = [ev for ev in result.events if ev["type"] == "tool_result"]
    assert [ev["name"] for ev in tool_results] == [
        "search_assets",
        "list_templates",
        "update_plan",
    ]
    assert all(ev["is_error"] is False for ev in tool_results)
    # La herramienta no paralelizable (update_plan) dejó el plan persistido
    assert store.get_active_plan(project["id"]) is not None


@pytest.mark.anyio
async def test_update_plan_creates_updates_and_cierra_plan(env) -> None:
    store, _broker, project, _stub_llm, _agent = env
    belt = create_default_tool_belt()
    ctx = ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=_agent._comfyui_client,
        broker=_broker,
        config=_agent._config,
    )

    created = await belt.execute(
        "update_plan",
        {
            "title": "Montar entrevista",
            "steps": [
                {"text": "Transcribir", "status": "in_progress"},
                {"text": "Generar EDL", "status": "pending"},
            ],
        },
        ctx,
    )
    plan_id = created["plan"]["id"]
    assert created["plan"]["status"] == "active"
    assert len(created["plan"]["steps"]) == 2

    updated = await belt.execute(
        "update_plan",
        {
            "title": "Montar entrevista",
            "steps": [
                {"text": "Transcribir", "status": "done"},
                {"text": "Generar EDL", "status": "in_progress"},
            ],
        },
        ctx,
    )
    assert updated["plan"]["id"] == plan_id
    assert updated["plan"]["steps"][0]["status"] == "done"

    closed = await belt.execute("update_plan", {"status": "completed", "plan_id": plan_id}, ctx)
    assert closed["plan"]["status"] == "completed"
    assert store.get_active_plan(project["id"]) is None


@pytest.mark.anyio
async def test_active_plan_and_summary_are_injected_into_system_message(env) -> None:
    store, _broker, project, stub_llm, agent = env
    store.save_plan(
        project["id"],
        title="Plan visible",
        steps=[{"text": "Paso uno", "status": "pending"}],
    )
    store.set_meta(project["id"], "chat_summary", {"text": "Resumen previo de prueba."})
    stub_llm.enqueue_step(StubLLMstep(content="Recibido."))

    await agent.run_turn(project_id=project["id"], user_message="continua")

    system_content = str(stub_llm.calls[-1][0]["content"])
    assert "Plan activo «Plan visible»" in system_content
    assert "Paso uno" in system_content
    assert "Resumen de la conversación previa" in system_content
    assert "Resumen previo de prueba." in system_content


@pytest.mark.anyio
async def test_llm_call_budget_stops_tool_loop(env) -> None:
    _store, _broker, project, stub_llm, _agent = env
    agent = StudioAgent(
        config=_agent._config,
        store=_store,
        comfyui_client=_agent._comfyui_client,
        broker=_broker,
        llm_client=OpenAICompatibleLLMClient(
            config=_agent._config,
            stub_client=stub_llm,
        ),
        max_llm_calls_per_turn=2,
    )
    for _ in range(5):
        stub_llm.enqueue_step(
            StubLLMstep(
                content="",
                tool_calls=[StubToolCallSpec(name="search_assets", arguments={"query": "x"})],
            )
        )

    result = await agent.run_turn(project_id=project["id"], user_message="loop infinito")

    assert result.stop_reason == "call_budget_exhausted"
    assert "presupuesto de llamadas LLM" in result.reply
    # Solo 2 llamadas LLM consumidas pese a tener 5 pasos guionizados
    tool_results = [ev for ev in result.events if ev["type"] == "tool_result"]
    assert len(tool_results) == 2


@pytest.mark.anyio
async def test_delegate_role_runs_nested_agent_with_restricted_belt(env) -> None:
    store, _broker, project, stub_llm, agent = env
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Delego en el editor.",
            tool_calls=[
                StubToolCallSpec(
                    name="delegate_role",
                    arguments={"role": "editor", "task": "Prepara un resumen del montaje."},
                ),
            ],
        )
    )
    stub_llm.enqueue_step(StubLLMstep(content="Hecho: EDL resumido por el editor."))
    stub_llm.enqueue_step(StubLLMstep(content="Delegación completada."))

    result = await agent.run_turn(project_id=project["id"], user_message="delega en editor")

    tool_results = [ev for ev in result.events if ev["type"] == "tool_result"]
    assert len(tool_results) == 1
    payload = tool_results[0]["result"]
    assert payload["role"] == "editor"
    assert payload["reply"] == "Hecho: EDL resumido por el editor."
    assert payload["stop_reason"] == "completed"

    # El sub-agente usó su prompt de rol y un tool-belt restringido (sin enqueue_render)
    nested_system = str(stub_llm.calls[1][0]["content"])
    assert "EDITOR/MONTADOR" in nested_system
    assert "build_edl" in nested_system
    assert "enqueue_render" not in nested_system

    # persist_memory=False: el sub-agente no persiste SU propio mensaje de usuario
    # (la tarea solo aparece dentro del tool_result del padre, que sí se persiste).
    user_contents = [
        str(m.get("content") or "")
        for m in store.list_chat_messages(project["id"], limit=100)
        if m.get("role") == "user"
    ]
    assert user_contents == ["delega en editor"]


@pytest.mark.anyio
async def test_delegate_role_unavailable_without_runner(env) -> None:
    _store, _broker, project, _stub_llm, agent = env
    belt = create_default_tool_belt()
    ctx = ToolContext(
        project_id=project["id"],
        store=_store,
        comfyui_client=agent._comfyui_client,
        broker=_broker,
        config=agent._config,
        delegate_runner=None,
    )
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "delegate_role",
            {"role": "director", "task": "renderiza algo"},
            ctx,
        )


def test_restricted_belt_subsets() -> None:
    base = create_default_tool_belt()
    director = create_restricted_belt(base, "director")
    editor = create_restricted_belt(base, "editor")
    unknown = create_restricted_belt(base, "rol-inexistente")

    assert "enqueue_render" in director.list_names()
    assert "build_edl" not in director.list_names()
    assert "build_edl" in editor.list_names()
    assert "enqueue_render" not in editor.list_names()
    assert "delegate_role" not in director.list_names()
    assert unknown.list_names() == []
