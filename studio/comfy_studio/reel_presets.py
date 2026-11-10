"""Versioned, bounded Remotion presets for programmed reels.

The cache and concurrency settings bound known Remotion resource consumers;
they are not a hard cap on total Chromium/FFmpeg RSS.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class ReelPreset:
    preset_id: str
    width: int
    height: int
    fps: int
    max_duration_s: float
    max_clips: int
    concurrency: int
    offthread_cache_bytes: int

    def public_dict(self) -> dict[str, int | float | str]:
        return asdict(self)


_PRESETS = {
    "social-vertical-v1": ReelPreset(
        "social-vertical-v1", 1080, 1920, 30, 60.0, 50, 2, 268_435_456
    ),
    "social-landscape-v1": ReelPreset(
        "social-landscape-v1", 1920, 1080, 30, 60.0, 50, 2, 268_435_456
    ),
    "square-preview-v1": ReelPreset(
        "square-preview-v1", 1080, 1080, 24, 30.0, 30, 1, 134_217_728
    ),
    "custom-preview-v1": ReelPreset(
        "custom-preview-v1", 1080, 1920, 30, 30.0, 30, 1, 134_217_728
    ),
}


def list_reel_presets() -> list[dict[str, int | float | str]]:
    return [_PRESETS[key].public_dict() for key in sorted(_PRESETS)]


def resolve_reel_preset(
    preset_id: str,
    *,
    width: int | None,
    height: int | None,
    fps: int | None,
    duration_s: float,
    clip_count: int,
) -> tuple[ReelPreset, int, int, int]:
    try:
        preset = _PRESETS[preset_id]
    except KeyError as exc:
        raise ValueError(f"preset de reel desconocido: {preset_id}") from exc
    resolved_width = width if width is not None else preset.width
    resolved_height = height if height is not None else preset.height
    resolved_fps = fps if fps is not None else preset.fps
    if resolved_width * resolved_height > 4_147_200:
        raise ValueError("el preset limita la salida a 4.147.200 píxeles por frame")
    target_ratio = {
        "social-vertical-v1": 9 / 16,
        "social-landscape-v1": 16 / 9,
        "square-preview-v1": 1.0,
    }.get(preset_id)
    if target_ratio is not None and abs(resolved_width / resolved_height - target_ratio) > 0.02:
        raise ValueError(f"la relación de aspecto no corresponde al preset {preset_id}")
    if resolved_fps > 30:
        raise ValueError("el preset limita la salida a 30 fps")
    if duration_s > preset.max_duration_s:
        raise ValueError(
            f"la duración total excede {preset.preset_id} ({preset.max_duration_s:g} s)"
        )
    if clip_count > preset.max_clips:
        raise ValueError(f"el preset limita el montaje a {preset.max_clips} clips")
    if duration_s * resolved_fps > 1500:
        raise ValueError("el preset limita el montaje a 1.500 frames")
    return preset, resolved_width, resolved_height, resolved_fps
