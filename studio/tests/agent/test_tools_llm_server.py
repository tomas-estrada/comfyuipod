"""Tests de la herramienta `local_llm_chat` (motor llm_server).

Sin GPU ni red: el modo stub da una completion simulada declarada y, sin binario
o sin modelo, la herramienta falla de forma tipada (nunca finge éxito).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.agent.tools import (
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def llm_tool_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ToolContext:
    monkeypatch.delenv("STUDIO_STUB_LLM_SERVER", raising=False)
    monkeypatch.delenv("STUDIO_LLM_SERVER_BIN", raising=False)
    monkeypatch.delenv("STUDIO_LLM_SERVER_MODEL", raising=False)
    monkeypatch.delenv("STUDIO_LLM_SERVER_URL", raising=False)
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto LLM Server")
    comfy_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=None,
        store=store,
        broker=broker,
        redactor=cfg.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=cfg,
    )


@pytest.mark.anyio
async def test_local_llm_chat_registrada() -> None:
    belt = create_default_tool_belt()
    spec = {s.name: s for s in belt.list_specs()}
    assert "local_llm_chat" in spec
    fields = set(spec["local_llm_chat"].args_model.model_fields)
    assert {"prompt", "messages", "model", "max_tokens", "temperature", "timeout_s"} <= fields


@pytest.mark.anyio
async def test_local_llm_chat_stub_devuelve_texto_simulado(
    llm_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_LLM_SERVER", "1")
    belt = create_default_tool_belt()
    result = await belt.execute(
        "local_llm_chat", {"prompt": "hola pod"}, llm_tool_context
    )
    assert result["status"] == "ok"
    assert result["simulated"] is True
    assert "hola pod" in result["text"]
    assert result["project_id"] == llm_tool_context.project_id


@pytest.mark.anyio
async def test_local_llm_chat_sin_binario_falla_tipado(
    llm_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sin stub y sin binario visible: el motor debe fallar tipado, no fingir.
    monkeypatch.setenv("PATH", "")
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError) as excinfo:
        await belt.execute("local_llm_chat", {"prompt": "hola"}, llm_tool_context)
    assert "llama-server no disponible" in str(excinfo.value)


@pytest.mark.anyio
async def test_local_llm_chat_rechaza_parametros_fuera_de_rango(
    llm_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_LLM_SERVER", "1")
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "local_llm_chat", {"prompt": "hola", "max_tokens": 0}, llm_tool_context
        )
