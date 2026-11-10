"""La herramienta `index_media` propaga el estado real/simulado (T3).

`ClipVisualIndex.index_asset` / `index_project_media` publican
`"simulated"` (True en sim/auto, False en real), pero `_handle_index_media`
reconstruia su propio dict de exito (`status/indexed`, conteos y planos) y
tiraba la bandera: un indexado simulado llegaba al agente como un `indexed`
indistinguible de uno real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.agent.tools import ToolContext, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.perception.clip_index import ClipVisualIndex
from comfy_studio.perception.face_index import InsightFaceIndex
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def index_tool_context(tmp_path: Path) -> ToolContext:
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Index Simulado")
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


def _patch_index_backends(monkeypatch: pytest.MonkeyPatch, *, simulated: bool) -> None:
    clip_payload = {
        "shots_indexed": 2,
        "shots": [{"id": "s1"}, {"id": "s2"}],
        "simulated": simulated,
        "simulation_notice": "x",
    }

    def _fake_index_asset(self, project_id, asset_id, **kwargs):
        return dict(clip_payload)

    def _fake_detect(self, project_id, asset_id, **kwargs):
        return [{"id": "d1"}]

    monkeypatch.setattr(ClipVisualIndex, "index_asset", _fake_index_asset)
    monkeypatch.setattr(InsightFaceIndex, "detect_and_index_asset", _fake_detect)


@pytest.mark.anyio
async def test_index_media_propagates_simulated_from_stub_backend(
    index_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_index_backends(monkeypatch, simulated=True)
    belt = create_default_tool_belt()
    res = await belt.execute(
        "index_media",
        {"asset_id": "a1", "detect_faces": True},
        index_tool_context,
    )
    assert res["status"] == "indexed"
    assert res.get("simulated") is True, "un indexado simulado no puede verse como exito real"


@pytest.mark.anyio
async def test_index_media_marks_real_backend_as_not_simulated(
    index_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_index_backends(monkeypatch, simulated=False)
    belt = create_default_tool_belt()
    res = await belt.execute(
        "index_media",
        {"asset_id": "a1", "detect_faces": True},
        index_tool_context,
    )
    assert res["status"] == "indexed"
    assert res.get("simulated") is False
    assert res["shots_indexed"] == 2
    assert res["faces_detected"] == 1
