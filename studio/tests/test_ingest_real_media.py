"""CPU integration fixture for the mixed-media ingestion contract."""

from __future__ import annotations

import math
import struct
import subprocess
import wave
from pathlib import Path

import pytest

from comfy_studio.ingest import AssetStatus, ingest_assets
from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.perception.keyframes import _make_solid_png, probe_media_info


def _make_video(path: Path, color: str, *, mobile_vfr: bool = False) -> Path:
    ffmpeg = find_ffmpeg()
    assert ffmpeg
    if mobile_vfr:
        source = path.with_name(f"{path.stem}.source.mp4")
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=48x32:r=30:d=1",
                "-vf",
                "select='not(eq(mod(n,3),1))'",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-output_ts_offset",
                "0.5",
                str(source),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-v",
                "error",
                "-display_rotation:v:0",
                "90",
                "-i",
                str(source),
                "-c",
                "copy",
                "-output_ts_offset",
                "0.5",
                str(path),
            ],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return path
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=32x32:r=2:d=0.5",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    return path


def _make_wav(path: Path, frequency: int) -> Path:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        samples = [
            int(12000 * math.sin(2 * math.pi * frequency * sample / 8000))
            for sample in range(800)
        ]
        output.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return path


@pytest.fixture
def mixed_real_media_corpus(tmp_path: Path) -> list[Path]:
    if not find_ffmpeg() or not find_ffprobe():
        pytest.skip("FFmpeg y ffprobe son necesarios para validar medios reales")

    paths: list[Path] = []
    paths.append(
        _make_video(tmp_path / "mobile_vfr_rotated.mp4", "red", mobile_vfr=True)
    )
    paths.append(_make_video(tmp_path / "clip_1.mp4", "blue"))
    for index, frequency in enumerate((440, 880)):
        paths.append(_make_wav(tmp_path / f"voice_{index}.wav", frequency))
    for index in range(94):
        rgb = (index, (index * 3) % 256, (index * 7) % 256)
        path = tmp_path / f"still_{index:03d}.png"
        path.write_bytes(_make_solid_png(rgb=rgb))
        paths.append(path)

    duplicate = tmp_path / "duplicate.png"
    duplicate.write_bytes(paths[4].read_bytes())
    paths.append(duplicate)
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"not a valid media container")
    paths.append(corrupt)
    assert len(paths) == 100
    return paths


def test_hundred_real_mixed_files_probe_dedup_and_isolate_corruption(
    mixed_real_media_corpus: list[Path],
) -> None:
    report = ingest_assets(
        mixed_real_media_corpus,
        project_id="mixed-corpus",
        weights_available=False,
        verify_media=True,
    )

    assert len(report.assets) == 100
    counts = report.counts()
    assert counts == {"pending_weights": 98, "duplicate": 1, "corrupt": 1}
    indexed_assets = report.by_status(AssetStatus.PENDING_WEIGHTS)
    assert len({asset.content_sha256 for asset in indexed_assets}) == 98
    assert all(
        asset.media_info and asset.media_info.get("streams")
        for asset in report.by_status(AssetStatus.PENDING_WEIGHTS)
    )
    mobile = next(
        asset for asset in report.by_status(AssetStatus.PENDING_WEIGHTS)
        if asset.source_path.endswith("mobile_vfr_rotated.mp4")
    )
    stream = next(item for item in mobile.media_info["streams"] if item["codec_type"] == "video")
    assert stream["avg_frame_rate"] != stream["r_frame_rate"]
    assert stream["start_time"] == "0.500000"
    assert stream["time_base"]
    assert stream["side_data_list"][0]["rotation"] == 90
    assert mobile.transforms["observed_rotation_degrees"] == 90
    assert mobile.transforms["frame_rate_relation"] == "different_rates_possible_vfr"
    probe = probe_media_info(mobile.source_path)
    assert probe["rotation_degrees"] == 90
    assert (probe["source_width"], probe["source_height"]) == (48, 32)
    assert (probe["width"], probe["height"]) == (32, 48)
    # FFmpeg's default autorotation turns the encoded 48x32 frame into a 32x48
    # display frame; the ingest metadata retains the unmodified source geometry.
    rendered = subprocess.run(
        [
            find_ffmpeg(),
            "-v",
            "error",
            "-i",
            mobile.source_path,
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=15,
    ).stdout
    assert rendered[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", rendered[16:24]) == (32, 48)
    for segment in mobile.segments:
        assert segment["source_asset_id"] == mobile.asset_id
        assert segment["source_path"] == mobile.source_path
        assert segment["source_interval_sec"] == {"start_sec": 0.5, "end_sec": 1.5}
    assert all(
        segment["status"] == "pending_weights"
        for asset in report.by_status(AssetStatus.PENDING_WEIGHTS)
        for segment in asset.segments
    )
    broken = report.by_status(AssetStatus.CORRUPT)
    assert len(broken) == 1
    assert "ffprobe" in (broken[0].error or "")


def test_duplicate_reference_is_a_valid_ffprobe_media_file(
    mixed_real_media_corpus: list[Path],
) -> None:
    report = ingest_assets(
        [mixed_real_media_corpus[4], mixed_real_media_corpus[-2]],
        weights_available=False,
        verify_media=True,
    )
    duplicate = report.by_status(AssetStatus.DUPLICATE)
    assert len(duplicate) == 1
    assert duplicate[0].duplicate_of


def test_ingest_configurable_asset_and_byte_limits_are_not_a_100_item_cap(
    mixed_real_media_corpus: list[Path],
) -> None:
    # A caller can deliberately request more than the A08 fixture size.
    report = ingest_assets(
        mixed_real_media_corpus,
        weights_available=False,
        verify_media=True,
        max_assets=120,
        max_total_bytes=1_000_000,
    )
    assert len(report.assets) == 100
    assert report.omitted_assets == 0
    assert report.counts()["pending_weights"] == 98

    bounded = ingest_assets(
        mixed_real_media_corpus,
        weights_available=False,
        verify_media=True,
        max_assets=10,
        max_total_bytes=100_000,
    )
    assert len(bounded.assets) == 10
    assert bounded.omitted_assets == 90
    assert bounded.counts()["limit_exceeded"] == 90


def test_ingest_byte_limit_skips_oversize_asset_and_continues(
    tmp_path: Path,
) -> None:
    small = _make_wav(tmp_path / "small.wav", 440)
    large = tmp_path / "large.wav"
    large.write_bytes(b"large" * 1000)
    later = _make_wav(tmp_path / "later.wav", 880)

    report = ingest_assets(
        [small, large, later],
        weights_available=False,
        max_total_bytes=small.stat().st_size + later.stat().st_size,
    )

    assert [asset.source_path for asset in report.assets] == [
        str(small),
        str(large),
        str(later),
    ]
    assert report.counts() == {"pending_weights": 2, "limit_exceeded": 1}
    assert report.assets[1].error == "asset exceeds configured ingest byte budget"

