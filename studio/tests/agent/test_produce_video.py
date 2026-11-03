"""Tests del orquestador produce_video: una instrucción -> clip largo ensamblado."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient  # noqa: F401  (import requerido por conftest)

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
def produce_context(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> ToolContext:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Produce Video")
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=studio_config,
    )


def _real_tiny_mp4() -> bytes:
    """MP4 real mínimo (1 s) para que el ensamblador de produce_video pase ffmpeg."""
    import shutil
    import subprocess
    import tempfile

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg requerido para produce_video (ensamblado real)"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "tiny.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=steelblue:s=320x180:d=1:r=16",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                str(out),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return out.read_bytes()


@pytest.mark.anyio
async def test_produce_video_renders_and_assembles_windows(
    produce_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # El stub ComfyUI produce un MP4 sintético no válido para ffmpeg; para el
    # ensamblado REAL lo sustituimos por un MP4 real mínimo.
    import comfy_studio.stubs.comfyui as stub_comfyui

    monkeypatch.setattr(stub_comfyui, "_SYNTHETIC_MP4", _real_tiny_mp4())
    belt = create_default_tool_belt()
    result = await belt.execute(
        "produce_video",
        {
            "story_prompt": "un niño salta a un coche rojo y conduce rápido al atardecer",
            "total_duration_sec": 12.0,
            "template_id": "wan_t2v",
        },
        produce_context,
    )
    assert result["status"] == "produced"
    assert result["assembled"] is True
    # 12 s a 81 frames/16 fps con solape 9 -> 3 ventanas
    assert len(result["windows"]) == 3
    for i, w in enumerate(result["windows"]):
        assert w["index"] == i
        assert w["asset_id"]
    final = result["output_asset"]
    assert final["kind"] == "video"
    meta = final.get("metadata") or {}
    assert meta.get("windowed_assembly") is True
    assert meta.get("chunks_count") == 3


@pytest.mark.anyio
async def test_produce_video_single_window_no_assembly(
    produce_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "produce_video",
        {
            "story_prompt": "plano corto de un coche girando en una esquina",
            "total_duration_sec": 4.0,
            "template_id": "wan_t2v",
        },
        produce_context,
    )
    assert result["status"] == "produced"
    assert result["assembled"] is False
    assert len(result["windows"]) == 1
    assert result["output_asset"]["kind"] == "video"


@pytest.mark.anyio
async def test_produce_video_fail_closed_on_unknown_template(
    produce_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="ventana 1/"):
        await belt.execute(
            "produce_video",
            {
                "story_prompt": "historia",
                "total_duration_sec": 6.0,
                "template_id": "plantilla_inexistente",
            },
            produce_context,
        )


def test_produce_video_registered_serialized() -> None:
    belt = create_default_tool_belt()
    spec = belt.get("produce_video")
    assert spec is not None
    assert spec.parallel_safe is False
    assert "produce_video" in {t for t in belt.list_names()}
