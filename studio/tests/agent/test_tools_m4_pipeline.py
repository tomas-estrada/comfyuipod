"""Tests de las herramientas de pipeline M4: planificador, voz, visión, identidades."""

from __future__ import annotations

import shutil
import subprocess
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

FFMPEG = shutil.which("ffmpeg")
HAS_FFMPEG = FFMPEG is not None


@pytest.fixture()
def pipeline_tool_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    # stubs de motores para no requerir modelos reales
    monkeypatch.setenv("STUDIO_STUB_MUSIC_GEN", "1")
    monkeypatch.setenv("STUDIO_STUB_VOICE_CLONE", "1")
    monkeypatch.setenv("STUDIO_STUB_LLM", "1")
    cfg = StudioConfig.from_env()
    assert cfg.studio_stub_llm is True
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Pipeline M4")
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


def _png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _upload_image(ctx: ToolContext) -> str:
    asset = ctx.store.save_upload(
        ctx.project_id,
        filename="ref.png",
        content_type="image/png",
        data=_png_bytes(),
        max_upload_bytes=8 * 1024 * 1024,
        category="uploads",
        metadata={},
    )
    return str(asset["id"])


# --- plan_long_render -------------------------------------------------------


@pytest.mark.anyio
async def test_plan_long_render_returns_windows(pipeline_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "plan_long_render", {"total_duration_sec": 20.0}, pipeline_tool_context
    )
    assert result["status"] == "planned"
    assert result["windows_count"] == 5
    assert result["windows"][0]["start_sec"] == 0.0
    assert result["windows"][0]["skip_first_frames"] == 0
    assert result["windows"][1]["skip_first_frames"] == 72
    # offsets coherentes con el avance neto (81-9 frames = 72)
    assert result["windows"][2]["skip_first_frames"] == 144
    # duración ensamblada ≈ objetivo
    assert abs(result["estimated_assembled_sec"] - 20.0) < 0.5
    assert "assemble_long_video" in result["next_step"]


@pytest.mark.anyio
async def test_plan_long_render_rejects_invalid(pipeline_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="chunk_frames"):
        await belt.execute(
            "plan_long_render",
            {"total_duration_sec": 10.0, "chunk_frames": 300},
            pipeline_tool_context,
        )


# --- generate_music / generate_speech (stub engines) ------------------------


@pytest.mark.anyio
async def test_generate_music_registers_audio_asset(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "generate_music",
        {"prompt": "música épica de persecución con sintetizadores", "duration_s": 12.0},
        pipeline_tool_context,
    )
    assert result["status"] == "generated"
    asset = result["output_asset"]
    assert asset["kind"] == "audio"
    assert (asset.get("metadata") or {}).get("engine") == "music_gen"


@pytest.mark.anyio
async def test_generate_speech_registers_voice_asset(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "generate_speech",
        {"text": "¡Vamos, sube al coche!", "pitch_shift": 2.0},
        pipeline_tool_context,
    )
    assert result["status"] == "generated"
    asset = result["output_asset"]
    assert asset["kind"] == "audio"
    meta = asset.get("metadata") or {}
    assert meta.get("engine") == "voice_clone"
    assert meta.get("text") == "¡Vamos, sube al coche!"


@pytest.mark.anyio
async def test_generate_speech_missing_reference_fails(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="no encontrado"):
        await belt.execute(
            "generate_speech",
            {"text": "hola", "reference_audio_asset_id": "no-existe"},
            pipeline_tool_context,
        )


# --- vision_check (stub LLM) ------------------------------------------------


@pytest.mark.anyio
async def test_vision_check_on_image_with_stub_llm(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    asset_id = _upload_image(pipeline_tool_context)
    result = await belt.execute(
        "vision_check",
        {"asset_id": asset_id, "question": "¿se ve un coche rojo?"},
        pipeline_tool_context,
    )
    assert result["status"] == "checked"
    assert result["frames_inspected"] == 1
    assert isinstance(result["answer"], str) and result["answer"]
    assert result["simulated"] is True


@pytest.mark.anyio
async def test_vision_check_missing_asset_fails(pipeline_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="no encontrado"):
        await belt.execute(
            "vision_check",
            {"asset_id": "no-existe", "question": "¿qué se ve?"},
            pipeline_tool_context,
        )


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg requerido")
@pytest.mark.anyio
async def test_vision_check_on_video_extracts_keyframes(
    pipeline_tool_context: ToolContext, tmp_path: Path
) -> None:
    belt = create_default_tool_belt()
    vid = tmp_path / "clip.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=320x180:d=4:r=16",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(vid),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    asset = pipeline_tool_context.store.save_upload(
        pipeline_tool_context.project_id,
        filename="clip.mp4",
        content_type="video/mp4",
        data=vid.read_bytes(),
        max_upload_bytes=64 * 1024 * 1024,
        category="uploads",
        metadata={},
    )
    result = await belt.execute(
        "vision_check",
        {"asset_id": str(asset["id"]), "question": "¿qué color domina?", "max_frames": 4},
        pipeline_tool_context,
    )
    assert result["status"] == "checked"
    assert 1 <= result["frames_inspected"] <= 4


# --- register_identity / list_identities -----------------------------------


@pytest.mark.anyio
async def test_register_and_list_identity_with_face_metadata(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    asset_id = _upload_image(pipeline_tool_context)
    # metadatos explícitos de cara para el modo simulado determinista
    pipeline_tool_context.store.update_asset_metadata(
        pipeline_tool_context.project_id,
        asset_id,
        metadata={
            "faces": [
                {"person": "Kid A", "bbox": [0.2, 0.1, 0.8, 0.9], "det_score": 0.98}
            ]
        },
    )
    result = await belt.execute(
        "register_identity",
        {"name": "Kid A", "reference_asset_id": asset_id},
        pipeline_tool_context,
    )
    assert result["status"] == "registered"
    identity = result["identity"]
    assert identity["name"] == "Kid A"
    assert identity["reference_asset_id"] == asset_id

    listing = await belt.execute("list_identities", {}, pipeline_tool_context)
    assert listing["status"] == "ok"
    assert listing["count"] == 1
    assert listing["identities"][0]["name"] == "Kid A"


@pytest.mark.anyio
async def test_register_identity_requires_valid_reference(
    pipeline_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="no encontrado"):
        await belt.execute(
            "register_identity",
            {"name": "Kid B", "reference_asset_id": "no-existe"},
            pipeline_tool_context,
        )


def test_pipeline_tools_registered_with_safety_flags() -> None:
    belt = create_default_tool_belt()
    for name, safe in [
        ("plan_long_render", True),
        ("generate_music", False),
        ("generate_speech", False),
        ("vision_check", False),
        ("register_identity", False),
        ("list_identities", True),
    ]:
        spec = belt.get(name)
        assert spec is not None, name
        assert spec.parallel_safe is safe, name


# --- upscale_asset ----------------------------------------------------------


@pytest.mark.anyio
async def test_upscale_asset_registers_upscaled_image(
    pipeline_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_UPSCALER", "1")
    belt = create_default_tool_belt()
    asset_id = _upload_image(pipeline_tool_context)
    result = await belt.execute(
        "upscale_asset", {"asset_id": asset_id, "scale": 4}, pipeline_tool_context
    )
    assert result["status"] == "upscaled"
    assert result["scale"] == 4
    asset = result["output_asset"]
    assert asset["kind"] == "image"
    meta = asset.get("metadata") or {}
    assert meta.get("engine") == "upscaler"
    assert meta.get("upscaled_from_asset_id") == asset_id
    assert "_x4_" in asset["filename"]


@pytest.mark.anyio
async def test_upscale_asset_rejects_non_image(pipeline_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="no encontrado"):
        await belt.execute("upscale_asset", {"asset_id": "no-existe"}, pipeline_tool_context)
