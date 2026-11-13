"""CPU temporal restoration baseline acceptance for C30/W08/W20."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from comfy_studio import temporal_restoration
from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.temporal_restoration import TemporalRestoreError, restore_video


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


@pytest.mark.parametrize(
    "rate,frames,duration",
    [
        ("nan", "12", "1"),
        ("12/1", "12", "nan"),
        ("1000000/1", "1000000", "1"),
        ("30/1", "100000", "1"),
    ],
)
def test_probe_rejects_nonfinite_or_unbounded_timeline(rate, frames, duration, monkeypatch) -> None:
    class Result:
        stdout = json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "width": 640,
                        "height": 360,
                        "duration": duration,
                        "avg_frame_rate": rate,
                        "r_frame_rate": rate,
                        "nb_frames": frames,
                    }
                ],
                "format": {},
            }
        )

    monkeypatch.setattr(temporal_restoration, "_run", lambda *args, **kwargs: Result())
    with pytest.raises(TemporalRestoreError):
        temporal_restoration._probe(Path("fixture.mp4"), "ffprobe")


def test_restore_rejects_source_over_byte_limit_before_probe(tmp_path, monkeypatch) -> None:
    source = tmp_path / "oversized.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr(temporal_restoration, "MAX_SOURCE_BYTES", 0)
    monkeypatch.setattr(
        temporal_restoration,
        "_probe",
        lambda *args: pytest.fail("ffprobe must not run on oversized input"),
    )
    with pytest.raises(TemporalRestoreError, match="source file exceeds"):
        restore_video(source, tmp_path / "output.mp4", ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")


def test_restore_rejects_and_cleans_oversized_output(tmp_path, monkeypatch) -> None:
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    source.write_bytes(b"source")
    monkeypatch.setattr(temporal_restoration, "MAX_OUTPUT_BYTES", 64)

    def fake_probe(path, _ffprobe):
        return {
            "width": 640 if path == source else 3840,
            "height": 360 if path == source else 2160,
            "duration_s": 1.0,
            "frame_rate": "12/1",
            "frame_count": 12,
            "time_base": "1/12",
            "has_audio": False,
        }

    def fake_run(argv, *, timeout, label):
        output.write_bytes(b"x" * 65)
        return None

    monkeypatch.setattr(temporal_restoration, "_probe", fake_probe)
    monkeypatch.setattr(temporal_restoration, "_run", fake_run)
    with pytest.raises(TemporalRestoreError, match="output file exceeds"):
        restore_video(source, output, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    assert not output.exists()


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
