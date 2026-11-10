from __future__ import annotations

import pytest

from comfy_studio.reel_presets import resolve_reel_preset


def test_preset_resolves_defaults_and_allows_safe_parameters() -> None:
    preset, width, height, fps = resolve_reel_preset(
        "social-vertical-v1",
        width=720,
        height=1280,
        fps=24,
        duration_s=12.0,
        clip_count=3,
    )
    assert preset.preset_id == "social-vertical-v1"
    assert (width, height, fps) == (720, 1280, 24)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"duration_s": 61.0, "clip_count": 1}, "duración total"),
        ({"duration_s": 5.0, "clip_count": 51}, "50 clips"),
        ({"duration_s": 5.0, "clip_count": 1, "width": 3840, "height": 2160}, "píxeles"),
        ({"duration_s": 5.0, "clip_count": 1, "fps": 60}, "30 fps"),
        ({"duration_s": 60.0, "clip_count": 1, "fps": 30}, "1.500 frames"),
        ({"duration_s": 5.0, "clip_count": 1, "width": 1280, "height": 720}, "relación de aspecto"),
    ],
)
def test_preset_enforces_duration_frame_pixel_and_clip_ceilings(kwargs, message) -> None:
    base = {
        "preset_id": "social-vertical-v1",
        "width": None,
        "height": None,
        "fps": None,
        "duration_s": 2.0,
        "clip_count": 1,
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        resolve_reel_preset(**base)


def test_unknown_preset_fails_closed() -> None:
    with pytest.raises(ValueError, match="desconocido"):
        resolve_reel_preset(
            "social-latest",
            width=None,
            height=None,
            fps=None,
            duration_s=1.0,
            clip_count=1,
        )
