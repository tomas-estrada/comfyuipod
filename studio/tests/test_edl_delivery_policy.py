"""Pruebas CPU para fps, rango y margen de imagen del render EDL."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from comfy_studio.edl.ffmpeg_edl import execute_edl_ffmpeg
from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.perception.keyframes import probe_media_info
from comfy_studio.projects import ProjectStore

FFMPEG = find_ffmpeg()
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="requiere ffmpeg")


@pytest.fixture()
def store_and_project(tmp_path: Path) -> tuple[ProjectStore, str]:
    store = ProjectStore(studio_data_dir=tmp_path / "studio")
    project = store.create_project(name="EDL delivery policy")
    return store, project["id"]


def _make_video(path: Path, source: str = "testsrc") -> bytes:
    video_input = (
        "color=c=black:s=128x72:r=24:d=2"
        if source == "black"
        else (
            "color=c=white:s=128x72:r=24:d=2"
            if source == "white"
            else "testsrc=duration=2:size=128x72:rate=24"
        )
    )
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            video_input,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path.read_bytes()


def _upload_source(store: ProjectStore, project_id: str, data: bytes) -> str:
    asset = store.save_upload(
        project_id,
        filename="source.mp4",
        content_type="video/mp4",
        data=data,
        max_upload_bytes=10 * 1024 * 1024,
        category="uploads",
    )
    return asset["id"]


def _save_edl(store: ProjectStore, project_id: str, asset_id: str) -> None:
    store.save_edl_version(
        project_id,
        edl={
            "schema": "comfy_studio.edl.v1",
            "title": "Delivery policy",
            "aspect_ratio": "16:9",
            "source_asset_id": asset_id,
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0}],
        },
    )


def _first_y(path: Path) -> int:
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "yuv420p",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    assert proc.stdout
    return proc.stdout[0]


def _rgb_at(path: Path, x: int, y: int) -> tuple[int, int, int]:
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    width = 1280
    offset = (y * width + x) * 3
    assert len(proc.stdout) >= offset + 3
    return tuple(proc.stdout[offset : offset + 3])


def test_edl_applies_fps_range_and_picture_inset(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "source.mp4", source="white")
    )
    _save_edl(store, project_id, source_id)

    result = execute_edl_ffmpeg(
        store,
        project_id,
        fps=12,
        color_range="full",
        picture_inset_percent=10.0,
    )
    output = Path(store.get_asset_path(project_id, result["output_asset"]["id"]))
    probe = probe_media_info(output)

    assert probe["probe_ok"] is True
    assert result["verification"]["fps"] == 12
    assert probe["fps"] == pytest.approx(12.0, abs=0.5)
    assert result["verification"]["color_range"] == "full"
    assert probe["color_range"] == "pc"
    assert result["verification"]["picture_inset_percent"] == 10.0
    assert _rgb_at(output, 0, 0) == (0, 0, 0)
    assert min(_rgb_at(output, 640, 360)) > 220


def test_color_range_converts_black_level_not_only_metadata(tmp_path, store_and_project):
    store, project_id = store_and_project
    source_id = _upload_source(
        store, project_id, _make_video(tmp_path / "black.mp4", source="black")
    )
    _save_edl(store, project_id, source_id)

    full = execute_edl_ffmpeg(store, project_id, color_range="full")
    limited = execute_edl_ffmpeg(store, project_id, color_range="limited")
    full_path = Path(store.get_asset_path(project_id, full["output_asset"]["id"]))
    limited_path = Path(store.get_asset_path(project_id, limited["output_asset"]["id"]))

    assert probe_media_info(full_path)["color_range"] == "pc"
    assert probe_media_info(limited_path)["color_range"] == "tv"
    assert _first_y(full_path) < _first_y(limited_path)


def test_invalid_delivery_policy_is_rejected_before_render(store_and_project):
    store, project_id = store_and_project
    with pytest.raises(ValueError, match="color_range"):
        execute_edl_ffmpeg(
            store, project_id, cuts=[{"in_sec": 0.0, "out_sec": 1.0}], color_range="p3"
        )
    with pytest.raises(ValueError, match="picture_inset_percent"):
        execute_edl_ffmpeg(
            store,
            project_id,
            cuts=[{"in_sec": 0.0, "out_sec": 1.0}],
            picture_inset_percent=26.0,
        )
    with pytest.raises(ValueError, match="fps"):
        execute_edl_ffmpeg(store, project_id, cuts=[{"in_sec": 0.0, "out_sec": 1.0}], fps=61)
