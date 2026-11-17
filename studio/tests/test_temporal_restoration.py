"""CPU temporal restoration baseline acceptance for C30/W08/W20."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from comfy_studio import temporal_restoration
from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.temporal_restoration import TemporalRestoreError, restore_video


def _write_pattern_sheet(path: Path) -> None:
    """Known synthetic sheet: binary 'C30' text glyphs plus a color product pattern."""
    glyphs = {
        "C": ("01110", "10000", "10000", "10000", "10000", "10000", "01110"),
        "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
        "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    }
    width, height, scale = 34, 36, 2
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
    # Product-like pattern: high-saturation 2px checkerboard below the text.
    for cy in range(7):
        for cx in range(7):
            color = b"\xff\x20\x20" if (cx + cy) % 2 == 0 else b"\xf0\xf0\xf0"
            for dy in range(scale):
                for dx in range(scale):
                    offset = ((20 + cy * scale + dy) * width + 4 + cx * scale + dx) * 3
                    pixels[offset : offset + 3] = color
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


def _probe_stdout(time_base: str | None) -> str:
    """Bounded synthetic ffprobe payload with a healthy timeline."""
    stream: dict[str, object] = {
        "codec_type": "video",
        "width": 640,
        "height": 360,
        "duration": "1",
        "avg_frame_rate": "12/1",
        "r_frame_rate": "12/1",
        "nb_frames": "12",
    }
    if time_base is not None:
        stream["time_base"] = time_base
    return json.dumps({"streams": [stream], "format": {}})


@pytest.mark.parametrize(
    ("time_base", "expected_timescale"),
    [("1/12288", 12288), ("1001/30000", 30000), ("2/48001", 48001)],
)
def test_probe_derives_video_track_timescale_from_rational_time_base(
    time_base, expected_timescale, monkeypatch
) -> None:
    monkeypatch.setattr(
        temporal_restoration,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(stdout=_probe_stdout(time_base)),
    )
    probed = temporal_restoration._probe(Path("fixture.mp4"), "ffprobe")
    assert probed["time_base"] == time_base
    assert probed["video_track_timescale"] == expected_timescale


@pytest.mark.parametrize(
    "time_base",
    [
        None,  # missing entirely
        "garbage",  # malformed fraction
        "1/0",  # non-finite denominator
        "0/1",  # non-positive
        "1/2000000",  # derived timescale outside the bounded range
    ],
)
def test_probe_rejects_time_bases_that_cannot_derive_a_timescale(
    time_base, monkeypatch
) -> None:
    monkeypatch.setattr(
        temporal_restoration,
        "_run",
        lambda *args, **kwargs: SimpleNamespace(stdout=_probe_stdout(time_base)),
    )
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
    captured: list[list[str]] = []

    def fake_probe(path, _ffprobe):
        return {
            "width": 640 if path == source else 3840,
            "height": 360 if path == source else 2160,
            "duration_s": 1.0,
            "frame_rate": "12/1",
            "frame_count": 12,
            "time_base": "1/12288",
            "video_track_timescale": 12288,
            "has_audio": False,
        }

    def fake_run(argv, *, timeout, label):
        captured.append(argv)
        output.write_bytes(b"x" * 65)
        return None

    monkeypatch.setattr(temporal_restoration, "_probe", fake_probe)
    monkeypatch.setattr(temporal_restoration, "_run", fake_run)
    with pytest.raises(TemporalRestoreError, match="output file exceeds"):
        restore_video(source, output, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    assert not output.exists()
    # The maskedmerge graph replaces passthrough video mapping entirely: only
    # the merged [vout] may be mapped, plus the derived source timescale.
    argv = captured[0]
    assert argv[argv.index("-map") + 1] == "[vout]"
    assert "[base][restored][edgemask]maskedmerge[vout]" in argv[argv.index("-filter_complex") + 1]
    assert argv[argv.index("-video_track_timescale") + 1] == "12288"
    assert argv[argv.index("-fps_mode") + 1] == "passthrough"
    assert "0:v:0" not in argv


def test_restore_rejects_frame_count_drift_and_removes_partial_output(
    tmp_path: Path, monkeypatch
) -> None:
    source, output = tmp_path / "source.mp4", tmp_path / "output.mp4"
    source.write_bytes(b"source")
    probes = 0

    def fake_probe(path, _ffprobe):
        nonlocal probes
        probes += 1
        return {
            "width": 640 if path == source.resolve() else 3840,
            "height": 360 if path == source.resolve() else 2160,
            "duration_s": 1.0,
            "frame_rate": "12/1",
            "frame_count": 12 if probes == 1 else 11,
            "time_base": "1/12288",
            "video_track_timescale": 12288,
            "has_audio": False,
        }

    def fake_run(argv, *, timeout, label):
        output.write_bytes(b"valid-sized-output")
        return None

    monkeypatch.setattr(temporal_restoration, "_probe", fake_probe)
    monkeypatch.setattr(temporal_restoration, "_run", fake_run)
    with pytest.raises(TemporalRestoreError, match="changed the source frame count"):
        restore_video(source, output, ffmpeg_bin="ffmpeg", ffprobe_bin="ffprobe")
    assert not output.exists()


@pytest.mark.skipif(not find_ffmpeg() or not find_ffprobe(), reason="requires FFmpeg/ffprobe")
def test_hd_pattern_restores_to_decodable_4k_and_reports_temporal_fidelity(tmp_path: Path) -> None:
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    assert ffmpeg and ffprobe
    source, reference, output = (tmp_path / n for n in ("hd.mp4", "known-4k.mp4", "restored.mp4"))
    text_pattern = tmp_path / "C30.ppm"
    _write_pattern_sheet(text_pattern)
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
def test_studio_route_rejects_reference_outside_project(tmp_path, client, app, monkeypatch) -> None:
    _use_small_output(monkeypatch)
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


def _build_known_synthetic_clip(
    ffmpeg: str, source: Path, pattern: Path, *, audio: bool
) -> None:
    """Known synthetic clip: moving bars + binary text/product pattern + optional tone.

    All inputs are declared before any output options: FFmpeg attaches options
    that precede an -i to that input, so interleaving output flags between
    inputs fails parsing.
    """
    argv = [
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
        str(pattern),
    ]
    if audio:
        argv += [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
        ]
    argv += [
        "-filter_complex",
        "[1:v]scale=170:180:flags=neighbor[pat];[0:v][pat]overlay=16:16[v]",
        "-map",
        "[v]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-frames:v",
        "12",
    ]
    if audio:
        argv += [
            "-map",
            "2:a",
            "-c:a",
            "aac",
            "-shortest",
        ]
    argv.append(str(source))
    subprocess.run(argv, check=True)


def _use_small_output(monkeypatch, width: int = 320, height: int = 180) -> None:
    """Keep the exact filter graph while bounding non-4K contract comparisons."""
    monkeypatch.setattr(temporal_restoration, "OUTPUT_WIDTH", width)
    monkeypatch.setattr(temporal_restoration, "OUTPUT_HEIGHT", height)
    monkeypatch.setattr(
        temporal_restoration,
        "RESTORATION_BRANCH",
        temporal_restoration.RESTORATION_BRANCH.replace(
            "3840:2160", f"{width}:{height}"
        ),
    )
    monkeypatch.setattr(
        temporal_restoration,
        "EDGE_MASK_GRAPH",
        temporal_restoration.EDGE_MASK_GRAPH.replace("3840:2160", f"{width}:{height}"),
    )


@pytest.mark.skipif(not find_ffmpeg() or not find_ffprobe(), reason="requires FFmpeg/ffprobe")
def test_protected_restoration_preserves_timing_text_edges_and_per_frame_metrics(
    tmp_path: Path, monkeypatch,
) -> None:
    """C30 protected-text CPU contract at bounded resolution."""
    _use_small_output(monkeypatch)
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    assert ffmpeg and ffprobe
    source, reference, output = (
        tmp_path / n for n in ("hd.mp4", "known-4k.mp4", "restored.mp4")
    )
    pattern = tmp_path / "pattern.ppm"
    _write_pattern_sheet(pattern)
    _build_known_synthetic_clip(ffmpeg, source, pattern, audio=True)
    # Known reference: plain Lanczos 4K upscale of the same clip, no denoise.
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"scale={temporal_restoration.OUTPUT_WIDTH}:{temporal_restoration.OUTPUT_HEIGHT}:flags=lanczos",
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
    # Explicit protected text/product-pattern handling is reported.
    assert report["profile"] == "hqdn3d-lanczos-unsharp-edge-masked-4k-v1"
    protection = report["protection"]
    assert protection["profile"] == "edge-masked-contour-protected-v1"
    assert protection["chroma_temporal_denoise"] == "disabled"
    assert protection["text_edge_recovery"] == "unsharp"
    assert "maskedmerge" in protection["contour_protection"]
    assert protection["graph"] == temporal_restoration.EDGE_MASK_GRAPH
    # Source timing preservation: rate, time base, derived timescale, exact
    # frame count and duration.
    before, after = report["source_timeline"], report["output_timeline"]
    assert before["frame_rate"] == after["frame_rate"]
    assert before["time_base"] == after["time_base"] == "1/12288"
    assert before["video_track_timescale"] == after["video_track_timescale"] == 12288
    assert before["frame_count"] == after["frame_count"] == 12
    assert before["duration_s"] == pytest.approx(after["duration_s"], abs=0.12)
    # Decodable 4K dimensions plus retained audio.
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
    assert (video["width"], video["height"]) == (
        temporal_restoration.OUTPUT_WIDTH,
        temporal_restoration.OUTPUT_HEIGHT,
    )
    assert any(s["codec_type"] == "audio" for s in streams)
    assert before["has_audio"] and after["has_audio"]
    # Per-frame temporal metrics: one transition per compared frame pair.
    temporal = report["temporal_metrics"]
    per_frame = temporal["per_frame_adjacent_delta"]
    assert len(per_frame) == temporal["frames_compared"] - 1
    assert all(delta >= 0 for delta in per_frame)
    # Text/edge fidelity: edges keep structure against the known reference.
    fidelity = report["fidelity_metrics"]
    assert fidelity["edge_pixels_compared"] > 0
    assert fidelity["edge_psnr_db"] > 20
    assert fidelity["flat_psnr_db"] > 20
    assert 0.8 <= fidelity["edge_structure_ratio"] <= 1.8
    assert "not SeedVR2" in report["limitations"]


@pytest.mark.skipif(not find_ffmpeg() or not find_ffprobe(), reason="requires FFmpeg/ffprobe")
def test_edge_masked_render_beats_unprotected_whole_frame_chain_on_edge_psnr(
    tmp_path: Path, monkeypatch,
) -> None:
    """C30 contour-protection acceptance against an unprotected control.

    The control renders the same restoration filters over the whole frame (the
    pre-C30 chain) instead of merging them through the edge mask, and both
    renders are scored against the same Lanczos 4K reference. Deterministic and
    bounded: a 12-frame synthetic clip at 320x180, fixed filters and capped metrics.
    """
    _use_small_output(monkeypatch)
    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    assert ffmpeg and ffprobe
    source, reference = tmp_path / "hd.mp4", tmp_path / "known-4k.mp4"
    protected, unprotected = tmp_path / "protected.mp4", tmp_path / "unprotected.mp4"
    pattern = tmp_path / "pattern.ppm"
    _write_pattern_sheet(pattern)
    _build_known_synthetic_clip(ffmpeg, source, pattern, audio=False)
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-vf",
            f"scale={temporal_restoration.OUTPUT_WIDTH}:{temporal_restoration.OUTPUT_HEIGHT}:flags=lanczos",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            str(reference),
        ],
        check=True,
    )
    report = restore_video(
        source, protected, reference=reference, ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe
    )
    # Unprotected control: identical restoration branch applied to every pixel
    # through plain -vf mapping, no edge mask and no maskedmerge.
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v",
            "-vf",
            temporal_restoration.RESTORATION_BRANCH,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            str(unprotected),
        ],
        check=True,
    )
    _, control_fidelity = temporal_restoration._metrics(
        source, unprotected, reference, ffmpeg
    )
    protected_fidelity = report["fidelity_metrics"]
    assert protected_fidelity["edge_pixels_compared"] > 0
    assert control_fidelity["edge_pixels_compared"] == protected_fidelity["edge_pixels_compared"]
    assert protected_fidelity["edge_psnr_db"] > 0
    assert control_fidelity["edge_psnr_db"] > 0
    assert protected_fidelity["edge_psnr_db"] > control_fidelity["edge_psnr_db"]
