"""Bounded CPU temporal restoration baseline for project-owned video assets.

This is deterministic FFmpeg denoise plus Lanczos resampling. It is not a
neural restoration model and makes no claim to recover missing source detail.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg, find_ffprobe

MAX_SOURCE_DURATION_S = 60.0
MAX_SOURCE_PIXELS = 3840 * 2160
OUTPUT_WIDTH, OUTPUT_HEIGHT = 3840, 2160


class TemporalRestoreError(ValueError):
    """Invalid source or failed bounded restoration operation."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(argv: list[str], *, timeout: float, label: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TemporalRestoreError(f"{label} failed: {exc}") from exc
    if result.returncode:
        raise TemporalRestoreError(f"{label} failed: {result.stderr[-800:]}")
    return result


def _probe(path: Path, ffprobe: str) -> dict[str, Any]:
    raw = _run(
        [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=20,
        label="ffprobe",
    )
    try:
        data = json.loads(raw.stdout)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        fmt = data.get("format", {})
        duration = float(video.get("duration") or fmt.get("duration") or 0)
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "duration_s": duration,
            "frame_rate": video.get("avg_frame_rate"),
            "time_base": video.get("time_base"),
            "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
        }
    except (ValueError, TypeError, KeyError, StopIteration) as exc:
        raise TemporalRestoreError("source must contain a readable video stream") from exc


def _gray_frames(path: Path, ffmpeg: str) -> bytes:
    argv = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-vf",
        "scale=160:90:flags=area,format=gray",
        "-frames:v",
        "720",
        "-f",
        "rawvideo",
        "-",
    ]
    try:
        result = subprocess.run(argv, capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TemporalRestoreError(f"frame metrics failed: {exc}") from exc
    if result.returncode:
        raise TemporalRestoreError(
            f"frame metrics failed: {result.stderr[-800:].decode(errors='replace')}"
        )
    return result.stdout


def _metrics(
    source: Path, output: Path, reference: Path, ffmpeg: str
) -> tuple[dict[str, float], dict[str, float]]:
    a, b, ref = (_gray_frames(p, ffmpeg) for p in (source, output, reference))
    frame_bytes = 160 * 90

    def frames(data: bytes) -> list[bytes]:
        return [
            data[i : i + frame_bytes] for i in range(0, len(data) - frame_bytes + 1, frame_bytes)
        ]

    af, bf, rf = frames(a), frames(b), frames(ref)
    n = min(len(af), len(bf), len(rf))
    if n < 2:
        raise TemporalRestoreError("source is too short for temporal/fidelity metrics")

    def adjacent(fs: list[bytes]) -> float:
        diffs = [
            abs(x - y)
            for left, right in zip(fs[: n - 1], fs[1:n], strict=True)
            for x, y in zip(left, right, strict=True)
        ]
        return sum(diffs) / len(diffs)

    squared = [
        (x - y) ** 2
        for left, right in zip(bf[:n], rf[:n], strict=True)
        for x, y in zip(left, right, strict=True)
    ]
    mse = sum(squared) / len(squared)
    psnr = 99.0 if mse == 0 else 10 * math.log10((255 * 255) / mse)
    return (
        {
            "adjacent_frame_delta": round(adjacent(bf), 5),
            "source_adjacent_frame_delta": round(adjacent(af), 5),
            "frames_compared": float(n),
        },
        {"reference_psnr_db": round(psnr, 5), "reference_frames_compared": float(n)},
    )


def restore_video(
    source: Path,
    output: Path,
    *,
    reference: Path | None = None,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
) -> dict[str, Any]:
    """Restore a short video using a fixed allowlisted FFmpeg filter chain."""
    ffmpeg, ffprobe = ffmpeg_bin or find_ffmpeg(), ffprobe_bin or find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise TemporalRestoreError("FFmpeg y ffprobe son requeridos")
    source, output = source.resolve(), output.resolve()
    if not source.is_file() or source == output:
        raise TemporalRestoreError("source/output path invalid")
    before = _probe(source, ffprobe)
    if before["duration_s"] <= 0 or before["duration_s"] > MAX_SOURCE_DURATION_S:
        raise TemporalRestoreError(f"source duration must be within 0..{MAX_SOURCE_DURATION_S}s")
    if before["width"] * before["height"] > MAX_SOURCE_PIXELS:
        raise TemporalRestoreError("source dimensions exceed CPU baseline limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            "hqdn3d=1.5:1.5:4:4,scale=3840:2160:flags=lanczos",
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
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout=max(60, before["duration_s"] * 30),
        label="FFmpeg CPU temporal baseline",
    )
    after = _probe(output, ffprobe)
    if (after["width"], after["height"]) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
        output.unlink(missing_ok=True)
        raise TemporalRestoreError("render did not produce exact 3840x2160 output")
    if before["has_audio"] and not after["has_audio"]:
        output.unlink(missing_ok=True)
        raise TemporalRestoreError("render lost source audio")
    report: dict[str, Any] = {
        "engine": "ffmpeg-cpu-temporal-baseline",
        "profile": "hqdn3d-lanczos-4k-v1",
        "source_sha256": _sha256(source),
        "output_sha256": _sha256(output),
        "source_timeline": before,
        "output_timeline": after,
        "temporal_metrics": {},
        "fidelity_metrics": {},
        "limitations": (
            "CPU resampling/denoise baseline; not SeedVR2 or FlashVSR neural restoration; "
            "does not recover detail absent from source."
        ),
    }
    if reference is not None:
        report["temporal_metrics"], report["fidelity_metrics"] = _metrics(
            source, output, reference.resolve(), ffmpeg
        )
    return report
