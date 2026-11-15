"""CPU contracts for the ACE-Step-backed music agent tool."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from comfy_studio import audio as audio_contract
from comfy_studio.agent import tools as agent_tools
from comfy_studio.agent.tools import ToolContext, ToolExecutionError, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture
def pipeline_tool_context(
    studio_config: StudioConfig, studio_data_dir: Path
) -> ToolContext:
    store = ProjectStore(studio_data_dir=studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="ACE-Step test")
    client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=None,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=client,
        broker=broker,
        config=studio_config,
    )


class _Registry:
    def __init__(self, *, omit_second_audio: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.omit_second_audio = omit_second_audio

    async def submit_render(self, template_id: str, params: dict[str, Any], **kwargs: Any):
        self.calls.append({"template_id": template_id, "params": params})
        job_id = f"ace-test-{len(self.calls)}"
        kwargs["store"].create_job(
            kind="render",
            job_id=job_id,
            project_id=kwargs["project_id"],
            template_id=template_id,
            status="done",
        )
        if self.omit_second_audio and len(self.calls) == 2:
            return {"status": "done", "job_id": job_id, "gallery_assets": []}
        asset = kwargs["store"].save_upload(
            kwargs["project_id"],
            filename=f"music-{len(self.calls)}.flac",
            content_type="audio/flac",
            data=b"fLaC\x00fixture",
            max_upload_bytes=1024,
            category="outputs",
            job_id=job_id,
        )
        return {"status": "done", "job_id": job_id, "gallery_assets": [asset]}


def _available_weights(ctx: ToolContext, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx.config.studio_include_music = True
    monkeypatch.setattr("comfy_studio.model_assets.flag_present", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        audio_contract,
        "_probe_streams",
        lambda *args, **kwargs: {
            "duration_s": 8.0,
            "sample_rate": 44100,
            "channels": 2,
        },
    )


@pytest.mark.anyio
async def test_generate_music_submits_sequential_variants_and_records_requested_grid(
    pipeline_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available_weights(pipeline_tool_context, monkeypatch)
    registry = _Registry()
    monkeypatch.setattr(agent_tools, "get_default_registry", lambda: registry)

    result = await create_default_tool_belt().execute(
        "generate_music",
        {
            "prompt": "warm analog synth cue",
            "structure": "build_release",
            "duration_s": 8,
            "bpm": 100,
            "variants": 2,
            "seed": 70,
        },
        pipeline_tool_context,
    )

    assert result["status"] == "generated"
    assert result["template_id"] == "ace_step_music"
    assert len(result["output_assets"]) == 2
    assert [call["params"]["seed"] for call in registry.calls] == [70, 71]
    assert all(call["template_id"] == "ace_step_music" for call in registry.calls)
    metadata = result["output_asset"]["metadata"]
    assert metadata["engine"] == "ace-step"
    assert metadata["beat_map_source"] == "requested_tempo_grid; audio alignment not measured"
    assert metadata["stems_separated"] is False
    assert "simulated" not in metadata


@pytest.mark.anyio
async def test_generate_music_rolls_back_assets_if_a_variant_has_no_audio(
    pipeline_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _available_weights(pipeline_tool_context, monkeypatch)
    registry = _Registry(omit_second_audio=True)
    monkeypatch.setattr(agent_tools, "get_default_registry", lambda: registry)

    with pytest.raises(ToolExecutionError, match="sin guardar una salida de audio"):
        await create_default_tool_belt().execute(
            "generate_music",
            {"prompt": "soft cue", "duration_s": 8, "variants": 2},
            pipeline_tool_context,
        )

    assert pipeline_tool_context.store.list_assets(pipeline_tool_context.project_id) == []
