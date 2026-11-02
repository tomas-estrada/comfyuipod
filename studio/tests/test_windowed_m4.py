"""Tests del ensamblador de clips largos por ventanas (M4): crossfade + audio."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from comfy_studio.edl.windowed import (
    MAX_WINDOW_CHUNKS,
    WindowedAssemblyError,
    assemble_windowed_video,
)
from comfy_studio.projects import ProjectStore

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos para los tests del ensamblador de ventanas",
)


def _make_test_video(path: Path, *, seconds: float = 2.0, color: str = "red") -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x180:d={seconds}:r=16",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.fixture()
def video_store(tmp_path: Path) -> ProjectStore:
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    store.create_project(name="Proyecto Ventanas")
    return store


def _upload_chunk(store: ProjectStore, project_id: str, path: Path) -> str:
    asset = store.save_upload(
        project_id,
        filename=path.name,
        content_type="video/mp4",
        data=path.read_bytes(),
        max_upload_bytes=64 * 1024 * 1024,
        category="outputs",
        metadata={"render_template": "test"},
    )
    return str(asset["id"])


def test_assemble_two_chunks_with_crossfade_and_audio(
    video_store: ProjectStore, tmp_path: Path
) -> None:
    project_id = video_store.list_projects()[0]["id"]
    c1, c2 = tmp_path / "c1.mp4", tmp_path / "c2.mp4"
    _make_test_video(c1, seconds=2.0, color="red")
    _make_test_video(c2, seconds=2.0, color="blue")
    a1, a2 = _upload_chunk(video_store, project_id, c1), _upload_chunk(video_store, project_id, c2)

    result = assemble_windowed_video(
        video_store,
        project_id,
        chunk_asset_ids=[a1, a2],
        crossfade_sec=0.5,
        audio_source_asset_id=a1,
    )
    assert result["status"] == "assembled"
    assert result["chunks_count"] == 2
    # 2s + 2s - 0.5s crossfade ≈ 3.5s
    dur = result["verification"]["actual_duration_sec"]
    assert 3.0 <= dur <= 4.0
    assert result["verification"]["has_audio"] is True
    assert result["verification"]["probe_ok"] is True
    assert len(result["applied_fades"]) == 1
    assert result["applied_fades"][0]["crossfade_sec"] == pytest.approx(0.5, abs=0.01)
    # El asset queda registrado en la galería con metadatos de ensamblado
    out = result["output_asset"]
    assert out["kind"] == "video"
    meta = out.get("metadata") or {}
    assert meta.get("windowed_assembly") is True
    assert meta.get("chunks_count") == 2


def test_assemble_single_chunk_passes_through(video_store: ProjectStore, tmp_path: Path) -> None:
    project_id = video_store.list_projects()[0]["id"]
    c1 = tmp_path / "c1.mp4"
    _make_test_video(c1, seconds=2.0, color="green")
    a1 = _upload_chunk(video_store, project_id, c1)
    result = assemble_windowed_video(
        video_store, project_id, chunk_asset_ids=[a1], crossfade_sec=0.0
    )
    assert result["status"] == "assembled"
    assert result["chunks_count"] == 1
    assert result["applied_fades"] == []
    dur = result["verification"]["actual_duration_sec"]
    assert 1.5 <= dur <= 3.0


def test_assemble_rejects_bad_inputs(video_store: ProjectStore, tmp_path: Path) -> None:
    project_id = video_store.list_projects()[0]["id"]
    with pytest.raises(WindowedAssemblyError, match="vacía"):
        assemble_windowed_video(video_store, project_id, chunk_asset_ids=[])
    with pytest.raises(WindowedAssemblyError, match="crossfade_sec"):
        assemble_windowed_video(
            video_store, project_id, chunk_asset_ids=["x"], crossfade_sec=5.0
        )
    with pytest.raises(WindowedAssemblyError, match="no encontrado"):
        assemble_windowed_video(video_store, project_id, chunk_asset_ids=["no-existe"])


def test_assemble_rejects_too_many_chunks(video_store: ProjectStore, tmp_path: Path) -> None:
    project_id = video_store.list_projects()[0]["id"]
    ids = [f"chunk_{i}" for i in range(MAX_WINDOW_CHUNKS + 1)]
    with pytest.raises(WindowedAssemblyError, match="máximo"):
        assemble_windowed_video(video_store, project_id, chunk_asset_ids=ids)


def test_assemble_long_video_tool_registered() -> None:
    from comfy_studio.agent.tools import create_default_tool_belt

    belt = create_default_tool_belt()
    assert belt.get("assemble_long_video") is not None
    spec = belt.get("assemble_long_video")
    assert spec is not None
    assert "crossfade" in spec.description.lower()
    assert spec.parallel_safe is False
