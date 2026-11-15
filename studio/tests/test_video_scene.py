"""Bounded CPU observation of source-video frame changes."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.spatial_memory import TemporalScene
from comfy_studio.video_scene import analyze_grayscale_frames


def _frame(square_x: int | None) -> Image.Image:
    image = Image.new("L", (160, 90), 0)
    if square_x is not None:
        ImageDraw.Draw(image).rectangle((square_x, 30, square_x + 15, 45), fill=255)
    return image


def test_observation_contains_source_bound_image_plane_change_keys() -> None:
    frames = [_frame(10), _frame(20), _frame(30)]
    artifact = analyze_grayscale_frames(
        frames,
        source_asset_id="clip_01",
        source_sha256="a" * 64,
        duration_s=1.0,
        sample_fps=2,
        source_timebase="24/1",
    )

    assert artifact["schema"] == "video-temporal-observation/1"
    assert artifact["source"] == {
        "asset_id": "clip_01",
        "kind": "project_video",
        "sha256": "a" * 64,
    }
    track = artifact["tracks"][0]
    assert track["id"] == "frame_change_1"
    assert track["coordinate_space"] == "normalized_image_plane"
    assert [key["t"] for key in track["keys"]] == [0.5, 1.0]
    assert all(0 <= value <= 1 for value in track["keys"][0]["center"])
    assert artifact["unknowns"]["depth"] == "unknown"
    assert artifact["unknowns"]["camera_pose"] == "unknown"
    scene = TemporalScene.from_dict(artifact["temporal_scene"])
    assert scene.objects["frame_change_1"].source_id == "clip_01"
    assert scene.sample_object("frame_change_1", 0.5)[2] == 0.0


def test_static_video_yields_no_fabricated_motion_track() -> None:
    artifact = analyze_grayscale_frames(
        [_frame(None), _frame(None)],
        source_asset_id="static_clip",
        source_sha256="b" * 64,
        duration_s=0.5,
        sample_fps=2,
        source_timebase="30/1",
    )
    assert artifact["tracks"] == []
    assert artifact["analysis"]["transitions_with_changes"] == 0


@pytest.mark.skipif(
    find_ffmpeg() is None or find_ffprobe() is None,
    reason="local ffmpeg/ffprobe required for decoded-video test",
)
def test_project_video_entrypoint_decodes_and_persists_observation(
    client: TestClient, tmp_path
) -> None:
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    video_path = tmp_path / "source clip.mp4"
    subprocess.run(
        [
            ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
            "testsrc2=size=64x64:rate=2:duration=1", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(video_path),
        ],
        check=True,
        capture_output=True,
    )
    project = client.post("/studio/api/projects", json={"name": "video-scene"})
    assert project.status_code in (200, 201)
    project_id = project.json()["id"]
    asset = client.app.state.store.save_upload(
        project_id,
        filename=video_path.name,
        content_type="video/mp4",
        data=video_path.read_bytes(),
        max_upload_bytes=1024 * 1024,
    )

    response = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/from-video",
        json={"asset_id": asset["id"], "sample_fps": 2},
    )
    assert response.status_code == 200, response.text
    observation = response.json()
    assert observation["source"]["asset_id"] == asset["id"]
    assert len(observation["source"]["sha256"]) == 64
    assert observation["analysis"]["sampled_frames"] >= 1
    assert observation["unknowns"]["camera_pose"] == "unknown"
    assert observation["temporal_scene"]["objects"]
    persisted = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/video-observation"
    )
    assert persisted.status_code == 200
    assert persisted.json() == observation

    other_project = client.post(
        "/studio/api/projects", json={"name": "other-video-scene"}
    ).json()["id"]
    foreign = client.post(
        f"/studio/api/projects/{other_project}/temporal-scene/from-video",
        json={"asset_id": asset["id"]},
    )
    assert foreign.status_code == 404
