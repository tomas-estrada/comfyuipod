"""Tests de las herramientas de auto-extensión del agente (M4): engines y modelos."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.agent.tools import (
    ToolContext,
    create_default_tool_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def engine_tool_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ToolContext:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Engines M4")
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
async def test_list_engines_returns_roster(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute("list_engines", {}, engine_tool_context)
    assert result["status"] == "ok"
    engines = result["engines"]
    assert engines["count"] >= 10
    names = {e["name"] for e in engines["engines"]}
    assert {"comfyui", "blender", "editor", "ffmpeg"}.issubset(names)
    # Pod determinista por defecto: dynamic_enabled False
    assert result["dynamic_enabled"] is False


@pytest.mark.anyio
async def test_install_engine_denied_without_dynamic(
    engine_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {"engine_name": "capture3d", "warm_attempted": True, "task_value": 10.0},
        engine_tool_context,
    )
    assert result["status"] == "denied"
    assert result["decision"]["allowed"] is False
    assert any("STUDIO_DYNAMIC" in r for r in result["decision"]["reasons"])


@pytest.mark.anyio
async def test_install_engine_denied_when_engine_already_warm(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {"engine_name": "blender", "warm_attempted": True, "task_value": 10.0},
        engine_tool_context,
    )
    assert result["status"] == "denied"
    assert any("ya está en el roster" in r for r in result["decision"]["reasons"])


@pytest.mark.anyio
async def test_install_engine_allowed_with_dynamic_and_value(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {
            "engine_name": "vgen_x",
            "warm_attempted": True,
            "task_value": 10.0,
            "est_idle_cost": 2.0,
            "source": "https://github.com/example/vgen_x",
        },
        engine_tool_context,
    )
    assert result["status"] == "allowed"
    assert result["decision"]["allowed"] is True


@pytest.mark.anyio
async def test_download_model_reports_animate_group(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute("download_model", {"model_group": "animate"}, engine_tool_context)
    assert result["model_group"] == "animate"
    assert result["templates"] == ["wan_animate"]
    # El config del test no activa STUDIO_INCLUDE_ANIMATE -> unavailable con instrucciones
    assert result["status"] in ("available", "unavailable")
    if result["status"] == "unavailable":
        assert "STUDIO_INCLUDE_ANIMATE=1" in result["reason"]


@pytest.mark.anyio
async def test_download_model_rejects_unknown_group(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError, match="Grupo de pesos desconocido"):
        await belt.execute("download_model", {"model_group": "minimax"}, engine_tool_context)


def test_engine_tools_registered_with_safety_flags() -> None:
    belt = create_default_tool_belt()
    list_spec = belt.get("list_engines")
    assert list_spec is not None and list_spec.parallel_safe is True
    install_spec = belt.get("install_engine")
    assert install_spec is not None and install_spec.parallel_safe is False
    dl_spec = belt.get("download_model")
    assert dl_spec is not None and dl_spec.parallel_safe is True
