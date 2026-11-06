"""Invariante: el resultado de una herramienta no revienta el contexto del LLM.

Causa (pre-fix): `loop.py` hacía `json.dumps(safe_res)` sin límite para
componer el mensaje `role=tool` y para persistirlo. Una herramienta como
`transcribe` (transcripción con segmentos y palabras), `list_templates`
con `include_workflow=True`, `export_subtitles` o `search_assets`
(100 assets) puede devolver cientos de KB que se acumulaban en cada
iteración hasta superar la ventana del modelo.

El test usa el `StubLLMClient` del proyecto con una herramienta de prueba
que devuelve un payload grande: el segundo mensaje enviado al modelo debe
ir truncado, no entero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from comfy_studio.agent.llm import OpenAICompatibleLLMClient
from comfy_studio.agent.loop import StudioAgent
from comfy_studio.agent.tools import ToolBelt, ToolContext, ToolSpec
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.llm import StubLLMClient, StubLLMstep, StubToolCallSpec

TAIL_MARKER = "COLA_UNICA_TRUNCABLE_987654321"
EXPECTED_MAX_TOOL_RESULT_CHARS = 16000


class _HugeArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _handle_huge(_args: _HugeArgs, _ctx: ToolContext) -> dict[str, Any]:
    return {"blob": "X" * 50000, "tail": TAIL_MARKER}


class _SmallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


async def _handle_small(_args: _SmallArgs, _ctx: ToolContext) -> dict[str, Any]:
    return {"ok": True}


def _belt() -> ToolBelt:
    belt = ToolBelt()
    belt.register(
        ToolSpec(
            name="huge_tool",
            description="Herramienta de prueba que devuelve un payload enorme.",
            args_model=_HugeArgs,
            handler=_handle_huge,
        )
    )
    belt.register(
        ToolSpec(
            name="small_tool",
            description="Herramienta de prueba con respuesta pequeña.",
            args_model=_SmallArgs,
            handler=_handle_small,
        )
    )
    return belt


@pytest.mark.anyio
async def test_huge_tool_result_is_truncated_before_llm(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto cap-result")
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Llamo a la herramienta enorme.",
            tool_calls=[StubToolCallSpec(name="huge_tool", arguments={})],
        )
    )
    stub_llm.enqueue_step(StubLLMstep(content="Listo."))
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
        tool_belt=_belt(),
    )

    result = await agent.run_turn(project_id=project["id"], user_message="usa la herramienta")

    assert result.stop_reason == "completed"
    assert result.reply == "Listo."
    assert len(stub_llm.calls) == 2
    second = stub_llm.calls[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    content = str(tool_msgs[0].get("content") or "")
    assert len(content) <= EXPECTED_MAX_TOOL_RESULT_CHARS + 200
    assert TAIL_MARKER not in content
    assert "truncad" in content.lower()

    # El historial persistido también está acotado (no se acumula sin límite).
    stored = store.list_chat_messages(project["id"], limit=50)
    tool_stored = [m for m in stored if m.get("role") == "tool"]
    assert tool_stored
    for msg in tool_stored:
        assert len(str(msg.get("content") or "")) <= EXPECTED_MAX_TOOL_RESULT_CHARS + 200

    # El evento `done` conserva el resultado completo para el cliente API.
    assert result.tool_results[0]["result"]["blob"] == "X" * 50000
    assert result.tool_results[0]["result"]["tail"] == TAIL_MARKER


@pytest.mark.anyio
async def test_small_tool_result_is_not_truncated(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto small-result")
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Llamo a la pequeña.",
            tool_calls=[StubToolCallSpec(name="small_tool", arguments={})],
        )
    )
    stub_llm.enqueue_step(StubLLMstep(content="Hecho."))
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
        tool_belt=_belt(),
    )

    result = await agent.run_turn(project_id=project["id"], user_message="usa la pequeña")

    assert result.stop_reason == "completed"
    second = stub_llm.calls[1]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    payload = json.loads(str(tool_msgs[0].get("content") or "{}"))
    assert payload == {"ok": True}
