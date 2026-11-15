"""ACE-Step's native API workflow remains pinned to repo assets and ComfyUI core."""

from __future__ import annotations

import pytest

from comfy_studio.templates import (
    TemplateParamError,
    TemplateRegistry,
    validate_template_definition,
)


def test_ace_step_template_is_a_bounded_native_audio_graph() -> None:
    registry = TemplateRegistry()
    template = registry.get_raw_definition("ace_step_music")

    assert validate_template_definition(template) == template
    assert template["output_kind"] == "audio"
    assert "ace_step_1.5_turbo_aio.safetensors" in template["models"]

    params = registry.validate_params(
        "ace_step_music",
        {"prompt": "warm instrumental cue", "duration_s": 20, "bpm": 120},
    )
    workflow = registry.render_workflow("ace_step_music", params)
    assert workflow["1"]["inputs"]["ckpt_name"] == "ace_step_1.5_turbo_aio.safetensors"
    assert workflow["3"]["inputs"]["tags"] == "hit_loop. warm instrumental cue"
    assert workflow["3"]["inputs"]["duration"] == 20
    assert workflow["3"]["inputs"]["bpm"] == 120
    assert workflow["7"]["class_type"] == "SaveAudioAdvanced"
    assert workflow["7"]["inputs"]["format"] == {"format": "flac"}


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"prompt": "  "}, "prompt"),
        ({"prompt": "cue", "duration_s": 0.5}, "duration_s"),
        ({"prompt": "cue", "duration_s": 301}, "duration_s"),
        ({"prompt": "cue", "bpm": 9}, "bpm"),
        ({"prompt": "cue", "bpm": 301}, "bpm"),
        ({"prompt": "cue", "seed": -1}, "seed"),
    ],
)
def test_ace_step_template_rejects_invalid_controls(
    params: dict[str, object], field: str
) -> None:
    registry = TemplateRegistry()
    with pytest.raises(TemplateParamError) as exc:
        registry.validate_params("ace_step_music", params)
    assert field in str(exc.value)


def test_ace_step_template_availability_tracks_real_weight_state() -> None:
    registry = TemplateRegistry()
    template = registry.get_raw_definition("ace_step_music")

    item = registry._format_entry(template, music_available=False)
    assert item["available"] is False
    assert "STUDIO_INCLUDE_MUSIC=1" in item["unavailable_reason"]
    assert registry._format_entry(template, music_available=True)["available"] is True


@pytest.mark.anyio
async def test_ace_step_submit_is_rejected_before_job_creation_without_weights() -> None:
    registry = TemplateRegistry()
    with pytest.raises(TemplateParamError, match="STUDIO_INCLUDE_MUSIC=1"):
        await registry.enqueue_render(
            "ace_step_music",
            {"prompt": "warm cue"},
            project_id=None,
            store=None,  # type: ignore[arg-type] -- the weight gate runs first
            comfyui_client=None,  # type: ignore[arg-type]
            strict=True,
            music_available=False,
        )
