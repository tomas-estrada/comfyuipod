"""La herramienta `diarize` propaga el estado real/simulado (T3).

`diarize_project_asset` publica `"simulated": True` a nivel superior (la
diarización actual es heurística espectral determinista, no inferencia real
de FunASR cam++), pero `_handle_diarize` reconstruía su propio dict de éxito
(`status/diarized`, hablantes, giros) y tiraba la bandera: una diarización
simulada llegaba al agente como `diarized` indistinguible de una real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import comfy_studio.asr.diarize as diarize_mod
from comfy_studio.agent.tools import ToolContext, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def diarize_tool_context(tmp_path: Path) -> ToolContext:
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Diarize Simulado")
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


def _patch_diarize_backend(monkeypatch: pytest.MonkeyPatch, *, simulated: bool) -> None:
    def _fake_diarize_project_asset(store, project_id, **kwargs):
        return {
            "transcription": {"id": "tr1", "asset_id": "a1"},
            "diarization": {"backend": "heuristic", "turns": []},
            "speakers": ["SPEAKER_00"],
            "by_speaker": {"SPEAKER_00": []},
            "simulated": simulated,
            "simulation_notice": "x",
        }

    monkeypatch.setattr(diarize_mod, "diarize_project_asset", _fake_diarize_project_asset)


@pytest.mark.anyio
async def test_diarize_propagates_simulated_from_heuristic_backend(
    diarize_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_diarize_backend(monkeypatch, simulated=True)
    belt = create_default_tool_belt()
    res = await belt.execute(
        "diarize",
        {"asset_id": "a1", "transcription_id": "tr1"},
        diarize_tool_context,
    )
    assert res["status"] == "diarized"
    assert res.get("simulated") is True, "una diarización simulada no puede verse como exito real"


@pytest.mark.anyio
async def test_diarize_marks_real_backend_as_not_simulated(
    diarize_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_diarize_backend(monkeypatch, simulated=False)
    belt = create_default_tool_belt()
    res = await belt.execute(
        "diarize",
        {"asset_id": "a1", "transcription_id": "tr1"},
        diarize_tool_context,
    )
    assert res["status"] == "diarized"
    assert res.get("simulated") is False
    assert res["speakers"] == ["SPEAKER_00"]
