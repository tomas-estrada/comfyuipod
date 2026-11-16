"""Bounded CPU temporal restoration baseline for project-owned video assets.

This is deterministic FFmpeg denoise plus Lanczos resampling with contour
protection: chroma-temporal denoising is disabled (it smears high-saturation
patterns across frames) and a denoise/unsharp restoration branch is merged in
only at detected edges through a localized edge mask (maskedmerge). It is not
a neural restoration model, performs no OCR or product recognition, and makes
no claim to recover missing source detail.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

from .media_bins import find_ffmpeg, find_ffprobe

MAX_SOURCE_DURATION_S = 60.0
MAX_SOURCE_PIXELS = 3840 * 2160
MAX_SOURCE_FPS = 30.0
MAX_SOURCE_FRAMES = 900
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
OUTPUT_WIDTH, OUTPUT_HEIGHT = 3840, 2160
METRIC_WIDTH, METRIC_HEIGHT = 160, 90
# Bounded metric work in pure Python: detailed per-frame and edge analysis is
# capped regardless of source length (frames beyond this are not compared).
MAX_METRICS_FRAMES = 120
# Reference local gradient magnitude (0..510) above which a pixel is an edge.
EDGE_GRADIENT_THRESHOLD = 12.0
# Chroma-temporal denoise (4th hqdn3d param) disabled: it smears saturated
# product-pattern pixels across frames. Unsharp recovers text/edge structure.
RESTORATION_BRANCH = (
    "format=yuv420p,hqdn3d=1.5:1.0:4:0,unsharp=5:5:0.55:5:5:0.0,"
    "scale=3840:2160:flags=lanczos,format=yuv420p"
)
# Localized edge mask: denoise/unsharp are applied only at detected contours.
# The unprocessed base survives everywhere else, so flat regions are never
# touched by the restoration filters. Output must be mapped via `-map [vout]`.
EDGE_MASK_GRAPH = (
    "[0:v]split=3[smooth][orig][mskin];"
    "[orig]scale=3840:2160:flags=lanczos,format=yuv420p[base];"
    f"[smooth]{RESTORATION_BRANCH}[restored];"
    "[mskin]scale=3840:2160:flags=lanczos,format=gray,"
    "edgedetect=low=0.04:high=0.12,dilation[edgemask];"
    "[base][restored][edgemask]maskedmerge[vout]"
)
PROTECTION_PROFILE = "edge-masked-contour-protected-v1"
# The denominator of the source time_base supplies a bounded MP4 track
# timescale, including non-unit rational ticks such as 1001/30000.
MIN_TRACK_TIMESCALE, MAX_TRACK_TIMESCALE = 1, 1_000_000


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
        if not math.isfinite(duration) or duration <= 0 or duration > MAX_SOURCE_DURATION_S:
            raise TemporalRestoreError(
                f"source duration must be finite and within 0..{MAX_SOURCE_DURATION_S}s"
            )
        rates: list[Fraction] = []
        for raw_rate in (video.get("avg_frame_rate"), video.get("r_frame_rate")):
            if raw_rate in (None, "", "N/A", "0/0"):
                continue
            try:
                rate = Fraction(str(raw_rate))
            except (ValueError, ZeroDivisionError) as exc:
                raise TemporalRestoreError("source frame rate is invalid or non-finite") from exc
            if rate <= 0:
                continue
            rates.append(rate)
        if not rates:
            raise TemporalRestoreError("source frame rate is missing or invalid")
        fps = max(float(rate) for rate in rates)
        if not math.isfinite(fps) or fps > MAX_SOURCE_FPS:
            raise TemporalRestoreError(f"source frame rate exceeds {MAX_SOURCE_FPS} fps")
        derived_frames = math.ceil(duration * fps)
        raw_frames = video.get("nb_frames")
        if raw_frames in (None, "", "N/A"):
            frame_count = derived_frames
        else:
            try:
                frame_count = max(int(raw_frames), derived_frames)
            except (ValueError, TypeError) as exc:
                raise TemporalRestoreError("source frame count is invalid") from exc
        if frame_count <= 0 or frame_count > MAX_SOURCE_FRAMES:
            raise TemporalRestoreError(f"source frame count exceeds {MAX_SOURCE_FRAMES}")
        raw_time_base = video.get("time_base")
        if not isinstance(raw_time_base, str) or not raw_time_base.strip():
            raise TemporalRestoreError("source time_base is missing or malformed")
        try:
            time_base = Fraction(raw_time_base.strip())
        except (ValueError, ZeroDivisionError) as exc:
            raise TemporalRestoreError("source time_base is invalid or non-finite") from exc
        if time_base <= 0:
            raise TemporalRestoreError("source time_base is invalid or non-finite")
        timescale = time_base.denominator
        if not MIN_TRACK_TIMESCALE <= timescale <= MAX_TRACK_TIMESCALE:
            raise TemporalRestoreError(
                "source time_base is outside the reasonable video track timescale range"
            )
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "duration_s": duration,
            "frame_rate": str(max(rates)),
            "frame_count": frame_count,
            "time_base": raw_time_base,
            "video_track_timescale": timescale,
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
        str(MAX_METRICS_FRAMES),
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
    frame_bytes = METRIC_WIDTH * METRIC_HEIGHT

    def frames(data: bytes) -> list[bytes]:
        return [
            data[i : i + frame_bytes] for i in range(0, len(data) - frame_bytes + 1, frame_bytes)
        ]

    af, bf, rf = frames(a), frames(b), frames(ref)
    n = min(len(af), len(bf), len(rf), MAX_METRICS_FRAMES)
    if n < 2:
        raise TemporalRestoreError("source is too short for temporal/fidelity metrics")

    w, h = METRIC_WIDTH, METRIC_HEIGHT
    # Per-frame adjacent transitions on the restored output: one bounded mean
    # absolute delta per compared frame pair.
    per_frame: list[float] = []
    for i in range(n - 1):
        left, right = bf[i], bf[i + 1]
        diffs = [abs(x - y) for x, y in zip(left, right, strict=True)]
        per_frame.append(sum(diffs) / len(diffs))

    squared = [
        (x - y) ** 2
        for left, right in zip(bf[:n], rf[:n], strict=True)
        for x, y in zip(left, right, strict=True)
    ]
    mse = sum(squared) / len(squared)
    psnr = 99.0 if mse == 0 else 10 * math.log10((255 * 255) / mse)

    # Text/edge fidelity: pixels classified as edge vs flat using local
    # horizontal/vertical gradients of the known reference frame.
    edge_sq = flat_sq = ref_edge_grad = out_edge_grad = 0.0
    edge_count = flat_count = 0
    for i in range(n):
        frame_out, frame_ref = bf[i], rf[i]
        for y in range(1, h - 1):
            row_base = y * w
            for x in range(1, w - 1):
                idx = row_base + x
                ref_grad = abs(frame_ref[idx + 1] - frame_ref[idx - 1]) + abs(
                    frame_ref[idx + w] - frame_ref[idx - w]
                )
                delta = frame_out[idx] - frame_ref[idx]
                if ref_grad >= EDGE_GRADIENT_THRESHOLD:
                    edge_sq += delta * delta
                    edge_count += 1
                    ref_edge_grad += ref_grad
                    out_edge_grad += abs(frame_out[idx + 1] - frame_out[idx - 1]) + abs(
                        frame_out[idx + w] - frame_out[idx - w]
                    )
                else:
                    flat_sq += delta * delta
                    flat_count += 1
    if edge_count == 0 or flat_count == 0:
        raise TemporalRestoreError("reference lacks edge or flat pixels for fidelity metrics")
    edge_psnr = 99.0 if edge_sq == 0 else 10 * math.log10((255 * 255) * edge_count / edge_sq)
    flat_psnr = 99.0 if flat_sq == 0 else 10 * math.log10((255 * 255) * flat_count / flat_sq)

    adjacent = sum(per_frame) / len(per_frame)
    diffs = [
        abs(x - y)
        for left, right in zip(af[: n - 1], af[1:n], strict=True)
        for x, y in zip(left, right, strict=True)
    ]
    return (
        {
            "adjacent_frame_delta": round(adjacent, 5),
            "per_frame_adjacent_delta": [round(delta, 5) for delta in per_frame],
            "source_adjacent_frame_delta": round(sum(diffs) / len(diffs), 5),
            "frames_compared": float(n),
        },
        {
            "reference_psnr_db": round(psnr, 5),
            "reference_frames_compared": float(n),
            "edge_pixels_compared": float(edge_count),
            "edge_psnr_db": round(edge_psnr, 5),
            "flat_psnr_db": round(flat_psnr, 5),
            "edge_structure_ratio": round(out_edge_grad / ref_edge_grad, 5),
        },
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
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise TemporalRestoreError(
            f"source file exceeds {MAX_SOURCE_BYTES} byte CPU baseline limit"
        )
    if output.exists():
        raise TemporalRestoreError("output path must not already exist")
    before = _probe(source, ffprobe)
    if before["width"] * before["height"] > MAX_SOURCE_PIXELS:
        raise TemporalRestoreError("source dimensions exceed CPU baseline limit")
    reference_path = reference.resolve() if reference is not None else None
    if reference_path is not None:
        if not reference_path.is_file() or reference_path.stat().st_size > MAX_SOURCE_BYTES:
            raise TemporalRestoreError(
                "reference file is missing or exceeds CPU baseline byte limit"
            )
        reference_probe = _probe(reference_path, ffprobe)
        if reference_probe["width"] * reference_probe["height"] > MAX_SOURCE_PIXELS:
            raise TemporalRestoreError("reference dimensions exceed CPU baseline limit")
    output.parent.mkdir(parents=True, exist_ok=True)
    completed = False
    try:
        _run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                str(source),
                "-map",
                "[vout]",
                "-map",
                "0:a?",
                "-filter_complex",
                EDGE_MASK_GRAPH,
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
                "-video_track_timescale",
                str(before["video_track_timescale"]),
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-fs",
                str(MAX_OUTPUT_BYTES + 1),
                str(output),
            ],
            timeout=max(60, before["duration_s"] * 30),
            label="FFmpeg CPU temporal baseline",
        )
        if not output.is_file():
            raise TemporalRestoreError("FFmpeg did not create an output file")
        if output.stat().st_size > MAX_OUTPUT_BYTES:
            raise TemporalRestoreError(
                f"output file exceeds {MAX_OUTPUT_BYTES} byte CPU baseline limit"
            )
        after = _probe(output, ffprobe)
        if (after["width"], after["height"]) != (OUTPUT_WIDTH, OUTPUT_HEIGHT):
            raise TemporalRestoreError("render did not produce exact 3840x2160 output")
        if after["frame_count"] != before["frame_count"]:
            raise TemporalRestoreError("render changed the source frame count")
        if Fraction(after["frame_rate"]) != Fraction(before["frame_rate"]):
            raise TemporalRestoreError("render changed the source frame rate")
        duration_tolerance_s = max(0.04, 1.0 / float(Fraction(before["frame_rate"])))
        if abs(after["duration_s"] - before["duration_s"]) > duration_tolerance_s:
            raise TemporalRestoreError("render changed the source timeline duration")
        if after["video_track_timescale"] != before["video_track_timescale"]:
            raise TemporalRestoreError("render changed the derived video track timescale")
        if before["has_audio"] and not after["has_audio"]:
            raise TemporalRestoreError("render lost source audio")
        report: dict[str, Any] = {
            "engine": "ffmpeg-cpu-temporal-baseline",
            "profile": "hqdn3d-lanczos-unsharp-edge-masked-4k-v1",
            "protection": {
                "profile": PROTECTION_PROFILE,
                "contour_protection": (
                    "maskedmerge edge mask: denoise/unsharp applied only at detected edges"
                ),
                "chroma_temporal_denoise": "disabled",
                "text_edge_recovery": "unsharp",
                "graph": EDGE_MASK_GRAPH,
            },
            "source_sha256": _sha256(source),
            "output_sha256": _sha256(output),
            "source_timeline": before,
            "output_timeline": after,
            "temporal_metrics": {},
            "fidelity_metrics": {},
            "limitations": (
                "CPU resampling/denoise baseline with localized edge-mask contour protection; "
                "not SeedVR2 or FlashVSR neural restoration; "
                "no OCR or product recognition; "
                "does not recover detail absent from source."
            ),
        }
        if reference_path is not None:
            report["temporal_metrics"], report["fidelity_metrics"] = _metrics(
                source, output, reference_path, ffmpeg
            )
        completed = True
        return report
    finally:
        if not completed:
            output.unlink(missing_ok=True)
