"""Tests CPU para el slice animatic C05/C08/A13."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from comfy_studio.animatic import (
    AnimaticError,
    check_animatic_preflight,
    encode_camera_trajectory_frames,
    interpolate_camera_at,
    render_animatic,
    sample_camera_path,
)
from comfy_studio.assets3d import Asset3D, Material, Part, Provenance
from comfy_studio.cinematography import CameraKeyframe, CameraPlan

_NEEDS_FFMPEG = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None


def _plan(**kw):
    defaults = {
        "shot_id": "animatic-test",
        "keyframes": [
            CameraKeyframe(t=0.0, position=(0.0, 1.0, 5.0), target=(0.0, 1.0, 0.0)),
            CameraKeyframe(t=1.0, position=(2.0, 1.0, 5.0), target=(0.0, 1.0, 0.0)),
        ],
        "focal_mm": 35.0,
        "duration_s": 1.0,
    }
    defaults.update(kw)
    return CameraPlan(**defaults)


def test_interpolation_endpoints_and_midpoint() -> None:
    plan = _plan()
    pos0, tgt0 = interpolate_camera_at(plan, 0.0)
    pos1, _ = interpolate_camera_at(plan, 1.0)
    posmid, _ = interpolate_camera_at(plan, 0.5)
    assert pos0 == (0.0, 1.0, 5.0)
    assert pos1 == (2.0, 1.0, 5.0)
    assert posmid == (1.0, 1.0, 5.0)


def test_sample_camera_path_count() -> None:
    plan = _plan()
    samples = sample_camera_path(plan, fps=6.0, duration_s=1.0)
    assert len(samples) == 6
    assert samples[0]["t"] == 0.0
    assert samples[-1]["t"] == 1.0


def test_animatic_sampling_rejects_unbounded_work() -> None:
    plan = _plan()
    with pytest.raises(AnimaticError, match="fps"):
        sample_camera_path(plan, fps=61.0, duration_s=1.0)
    with pytest.raises(AnimaticError, match="frame_count"):
        sample_camera_path(plan, frame_count=901, duration_s=1.0)
    with pytest.raises(AnimaticError, match="duration_s"):
        sample_camera_path(plan, fps=1.0, duration_s=121.0)


def test_animatic_rejects_nonfinite_camera_coordinates() -> None:
    plan = _plan(
        keyframes=[
            CameraKeyframe(t=0.0, position=(float("nan"), 1.0, 5.0), target=(0.0, 0.0, 0.0))
        ]
    )
    with pytest.raises(AnimaticError, match="finitos"):
        sample_camera_path(plan, fps=1.0, duration_s=1.0)


def test_preflight_ok_for_safe_plan() -> None:
    plan = _plan()
    pf = check_animatic_preflight(plan, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)
    assert pf["ok"] is True
    assert pf["composition"]["ok"] is True


def test_preflight_detects_collision() -> None:
    kf = CameraKeyframe(t=0.0, position=(0.0, 1.0, 0.1), target=(0.0, 1.0, 0.0))
    plan = _plan(keyframes=[kf])
    pf = check_animatic_preflight(plan, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)
    assert pf["ok"] is False
    assert pf["composition"]["collisions"]


def test_preflight_factual_rejects_inferred() -> None:
    plan = _plan(factual=True)
    mats = [Material(name="m", provenance=Provenance.OBSERVED)]
    parts = [Part(part_id="back", name="hidden", material="m", provenance=Provenance.INFERRED)]
    asset = Asset3D(asset_id="a", name="a", materials=mats, parts=parts)
    pf = check_animatic_preflight(plan, asset=asset, part_id="back")
    assert pf["ok"] is False
    assert not pf["factual"]["ok"]


def test_preflight_factual_requires_evidence() -> None:
    pf = check_animatic_preflight(_plan(factual=True))
    assert pf["ok"] is False
    assert pf["factual"]["checked"] is True


def test_render_camera_animatic_tool_is_registered_with_clear_scope() -> None:
    from comfy_studio.agent.tools import create_default_tool_belt

    spec = create_default_tool_belt().get("render_camera_animatic")
    assert spec is not None
    description = (spec.description or "").lower()
    assert "sin asset_id" in description and "glb" in description
    assert "blender cycles cpu" in description


@pytest.mark.skipif(_NEEDS_FFMPEG, reason="requiere ffmpeg/ffprobe")
def test_render_animatic_produces_verified_mp4(tmp_path: Path) -> None:
    plan = _plan()
    out = tmp_path / "animatic.mp4"
    result = render_animatic(plan, out, fps=6.0, width=320, height=180, duration_s=1.0)
    assert Path(result["output"]).is_file()
    assert result["probe_ok"] is True
    assert result["frames"] == 6
    assert result["width"] == 320 and result["height"] == 180
    assert result["interpolated_keys"]
    probe = result["probe"]
    assert probe["has_video"] is True
    assert probe["width"] == 320 and probe["height"] == 180


@pytest.mark.skipif(_NEEDS_FFMPEG, reason="requiere ffmpeg/ffprobe")
def test_render_animatic_preflight_blocks_bad_plan(tmp_path: Path) -> None:
    kf = CameraKeyframe(t=0.0, position=(0.0, 1.0, 0.1), target=(0.0, 1.0, 0.0))
    plan = _plan(keyframes=[kf])
    out = tmp_path / "bad.mp4"
    with pytest.raises(AnimaticError):
        render_animatic(plan, out, subject_position=(0.0, 1.0, 0.0), subject_radius=0.5)


def test_render_animatic_rejects_excessive_dimensions(tmp_path: Path) -> None:
    with pytest.raises(AnimaticError, match="dimensiones"):
        render_animatic(_plan(), tmp_path / "bad.mp4", width=8192, height=8192)


@pytest.mark.skipif(_NEEDS_FFMPEG, reason="requiere ffmpeg/ffprobe")
def test_encode_camera_trajectory_frames_probes_frame_count_and_dimensions(
    tmp_path: Path,
) -> None:
    from PIL import Image

    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    for index, color in enumerate(((220, 40, 30), (30, 80, 220)), start=1):
        Image.new("RGB", (320, 180), color).save(render_dir / f"render_{index:04d}.png")

    result = encode_camera_trajectory_frames(
        [render_dir / "render_0001.png", render_dir / "render_0002.png"],
        tmp_path / "camera.mp4",
        fps=2,
        width=320,
        height=180,
    )
    assert result["probe_ok"] is True
    assert result["frame_count"] == 2
    assert result["width"] == 320 and result["height"] == 180
    assert result["has_video"] is True and result["has_audio"] is False
    assert result["duration_s"] == pytest.approx(1.0, abs=0.1)


@pytest.mark.skipif(_NEEDS_FFMPEG, reason="requiere ffmpeg/ffprobe")
def test_encode_camera_trajectory_frames_rejects_corrupt_or_wrong_size_png(
    tmp_path: Path,
) -> None:
    render_dir = tmp_path / "renders"
    render_dir.mkdir()
    (render_dir / "render_0001.png").write_bytes(b"not a png")
    (render_dir / "render_0002.png").write_bytes(b"not a png")

    with pytest.raises(AnimaticError, match="PNG de cámara inválido"):
        encode_camera_trajectory_frames(
            [render_dir / "render_0001.png", render_dir / "render_0002.png"],
            tmp_path / "camera.mp4",
            fps=2,
            width=320,
            height=180,
        )
