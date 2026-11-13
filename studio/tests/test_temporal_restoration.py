"""CPU temporal restoration baseline acceptance for C30/W08/W20."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.temporal_restoration import restore_video


def _write_text_pattern(path: Path) -> None:
    glyphs = {
        "C": ("01110", "10000", "10000", "10000", "10000", "10000", "01110"),
        "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
        "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    }
    width, height, scale = 34, 18, 2
    pixels = bytearray(width * height * 3)
    for index, char in enumerate("C30"):
        for y, row in enumerate(glyphs[char]):
            for x, bit in enumerate(row):
                if bit == "1":
                    for dy in range(scale):
                        for dx in range(scale):
                            offset = (
                                (2 + y * scale + dy) * width + 2 + index * 10 + x * scale + dx
                            ) * 3
                            pixels[offset : offset + 3] = b"\xff\xff\xff"
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)


@pytest.mark.skipif(not find_ffmpeg() or not find_ffprobe(), reason="requires FFmpeg/ffprobe")
def test_hd_pattern_restores_to_decodable_4k_and_reports_temporal_fidelity(tmp_path: Path) -> None:
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    assert ffmpeg and ffprobe
    source, reference, output = (tmp_path / n for n in ("hd.mp4", "known-4k.mp4", "restored.mp4"))
    text_pattern = tmp_path / "C30.ppm"
    _write_text_pattern(text_pattern)
    # Moving high-contrast bars, known pixel text, and a known tone exercise
    # inter-frame stability, retained spatial structure, and audio passthrough.
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=12:duration=1",
            "-loop",
            "1",
            "-framerate",
            "12",
            "-i",
            str(text_pattern),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-filter_complex",
            "[0:v][1:v]overlay=10:10[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            "scale=3840:2160:flags=lanczos",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-c:a",
            "copy",
            str(reference),
        ],
        check=True,
    )
    report = restore_video(
        source, output, reference=reference, ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe
    )
    probed = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(probed.stdout)["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (3840, 2160)
    assert any(s["codec_type"] == "audio" for s in streams)
    assert report["engine"] == "ffmpeg-cpu-temporal-baseline"
    assert report["source_sha256"] and report["output_sha256"]
    assert report["temporal_metrics"]["adjacent_frame_delta"] >= 0
    assert report["fidelity_metrics"]["reference_psnr_db"] > 20
    assert report["source_timeline"]["duration_s"] == pytest.approx(
        report["output_timeline"]["duration_s"], abs=0.12
    )
    assert "not SeedVR2" in report["limitations"]


@pytest.mark.skipif(not find_ffmpeg(), reason="requires FFmpeg")
def test_studio_route_rejects_reference_outside_project(tmp_path, client, app) -> None:
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    source_path = tmp_path / "owned.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=2:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source_path),
        ],
        check=True,
    )
    projects = []
    for name in ("owner", "other"):
        response = client.post("/studio/api/projects", json={"name": name})
        assert response.status_code in (200, 201)
        projects.append(response.json()["id"])
    store = app.state.store
    source = store.save_upload(
        projects[0],
        filename="source.mp4",
        content_type="video/mp4",
        data=source_path.read_bytes(),
        max_upload_bytes=1024 * 1024,
    )
    reference = store.save_upload(
        projects[1],
        filename="reference.mp4",
        content_type="video/mp4",
        data=source_path.read_bytes(),
        max_upload_bytes=1024 * 1024,
    )
    response = client.post(
        f"/studio/api/projects/{projects[0]}/assets/{source['id']}/restore-temporal",
        json={"asset_id": source["id"], "reference_asset_id": reference["id"]},
    )
    assert response.status_code == 404
    response = client.post(
        f"/studio/api/projects/{projects[0]}/assets/{source['id']}/restore-temporal",
        json={"asset_id": source["id"]},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    metadata = payload["asset"]["metadata"]["temporal_restoration_v1"]
    assert payload["asset"]["kind"] == "video"
    assert metadata["source_asset_id"] == source["id"]
    assert metadata["source_sha256"] == payload["restoration"]["source_sha256"]
    assert metadata["output_sha256"] == payload["restoration"]["output_sha256"]
    assert payload["restoration"]["engine"] == "ffmpeg-cpu-temporal-baseline"
