"""Bounded, CPU-only camera-motion estimates from tiny grayscale frame samples.

This is a deterministic block-matching heuristic, not optical flow or a
real-footage quality guarantee. It reports only broad per-shot labels and keeps
ambiguous or textureless footage unknown.
"""

from __future__ import annotations

import math
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path

METHOD = "sparse-block-match-v1"
FRAME_WIDTH = 64
FRAME_HEIGHT = 36
MAX_FRAMES = 120
MAX_DURATION_S = 600.0
DEFAULT_TIMEOUT_S = 30.0
BLOCK_SIZE = 6
BLOCK_STEP = 8
SEARCH_RADIUS = 3
MIN_TEXTURE_VARIANCE = 30.0
MIN_BLOCKS = 8


@dataclass(frozen=True, slots=True)
class CameraMotionEstimate:
    """One conservative camera movement label and supporting CPU evidence."""

    move: str
    confidence: float
    frames_used: int
    blocks_used: int
    method: str = METHOD

    def to_dict(self) -> dict[str, str | float | int]:
        return {
            "move": self.move,
            "origin": "inferred" if self.move != "unknown" else "unknown",
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "frames_used": self.frames_used,
            "blocks_used": self.blocks_used,
        }


def decode_grayscale_frames(
    path: Path | str,
    *,
    ffmpeg_bin: str,
    duration_s: float,
    max_frames: int = MAX_FRAMES,
    width: int = FRAME_WIDTH,
    height: int = FRAME_HEIGHT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[list[tuple[float, bytes]], float]:
    """Decode bounded grayscale samples; timestamps use the requested CFR."""
    if not math.isfinite(duration_s) or duration_s <= 0:
        return [], 0.0
    if not 1 <= width <= 128 or not 1 <= height <= 128:
        raise ValueError("camera-motion sample dimensions must be within 1..128")
    if not 2 <= max_frames <= MAX_FRAMES or timeout_s <= 0:
        raise ValueError("camera-motion sample limits are invalid")

    sampled_duration = min(float(duration_s), MAX_DURATION_S)
    rate = min(6.0, max_frames / sampled_duration)
    command = [
        ffmpeg_bin,
        "-v",
        "error",
        "-threads",
        "1",
        "-i",
        str(path),
        "-t",
        f"{sampled_duration:.3f}",
        "-vf",
        (
            f"fps={rate:.6f},scale={width}:{height}:"
            "force_original_aspect_ratio=decrease:flags=area,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=gray"
        ),
        "-frames:v",
        str(max_frames),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        timeout=timeout_s,
    )
    frame_size = width * height
    raw = result.stdout
    if result.returncode != 0 or len(raw) < frame_size * 2:
        return [], rate
    count = min(max_frames, len(raw) // frame_size)
    frames = [
        (index / rate, raw[index * frame_size : (index + 1) * frame_size])
        for index in range(count)
    ]
    return frames, rate


def estimate_camera_motion(
    frames: list[bytes] | tuple[bytes, ...],
    width: int,
    height: int,
) -> CameraMotionEstimate:
    """Classify a sequence as static, pan, zoom, or unknown from block motion."""
    valid = (
        1 <= width <= 128
        and 1 <= height <= 128
        and len(frames) >= 2
        and all(len(frame) == width * height for frame in frames)
    )
    if not valid:
        return CameraMotionEstimate("unknown", 0.0, len(frames), 0)

    vectors: list[tuple[float, float, float, float]] = []
    for first, second in zip(frames, frames[1:], strict=False):
        vectors.extend(_block_vectors(first, second, width, height))
    if len(vectors) < MIN_BLOCKS:
        return CameraMotionEstimate("unknown", 0.0, len(frames), len(vectors))

    total = len(vectors)
    stationary = sum(math.hypot(dx, dy) <= 0.75 for dx, dy, _, _ in vectors)
    stationary_support = stationary / total
    if stationary_support >= 0.62:
        return CameraMotionEstimate("static", stationary_support, len(frames), total)

    median_dx = statistics.median(vector[0] for vector in vectors)
    median_dy = statistics.median(vector[1] for vector in vectors)
    median_magnitude = math.hypot(median_dx, median_dy)
    translation_inliers = sum(
        abs(dx - median_dx) <= 1.0 and abs(dy - median_dy) <= 1.0
        for dx, dy, _, _ in vectors
    )
    translation_support = translation_inliers / total

    horizontal_vectors: list[bool] = []
    vertical_vectors: list[bool] = []
    for dx, dy, x, y in vectors:
        radius_x = x - (width - 1) / 2
        radius_y = y - (height - 1) / 2
        if abs(radius_x) >= width * 0.15 and abs(dx) >= 0.8:
            horizontal_vectors.append(dx * radius_x > 0)
        if abs(radius_y) >= height * 0.15 and abs(dy) >= 0.8:
            vertical_vectors.append(dy * radius_y > 0)
    horizontal_zoom_support = (
        max(sum(horizontal_vectors), len(horizontal_vectors) - sum(horizontal_vectors))
        / len(horizontal_vectors)
        if horizontal_vectors
        else 0.0
    )
    vertical_zoom_support = (
        max(sum(vertical_vectors), len(vertical_vectors) - sum(vertical_vectors))
        / len(vertical_vectors)
        if vertical_vectors
        else 0.0
    )
    zoom_support = min(horizontal_zoom_support, vertical_zoom_support)
    if zoom_support >= 0.62:
        return CameraMotionEstimate("zoom", zoom_support, len(frames), total)
    if median_magnitude >= 0.8 and translation_support >= 0.6:
        return CameraMotionEstimate("pan", translation_support, len(frames), total)

    return CameraMotionEstimate(
        "unknown",
        max(stationary_support, translation_support, zoom_support),
        len(frames),
        total,
    )


def _block_vectors(
    first: bytes,
    second: bytes,
    width: int,
    height: int,
) -> list[tuple[float, float, float, float]]:
    vectors: list[tuple[float, float, float, float]] = []
    min_origin = SEARCH_RADIUS
    max_x = width - BLOCK_SIZE - SEARCH_RADIUS
    max_y = height - BLOCK_SIZE - SEARCH_RADIUS
    for y in range(min_origin, max_y + 1, BLOCK_STEP):
        for x in range(min_origin, max_x + 1, BLOCK_STEP):
            values = [
                first[(y + row) * width + x + col]
                for row in range(BLOCK_SIZE)
                for col in range(BLOCK_SIZE)
            ]
            mean = sum(values) / len(values)
            variance = sum((value - mean) ** 2 for value in values) / len(values)
            if variance < MIN_TEXTURE_VARIANCE:
                continue

            best_cost: int | None = None
            second_best_cost: int | None = None
            best_dx = best_dy = 0
            for dy in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
                for dx in range(-SEARCH_RADIUS, SEARCH_RADIUS + 1):
                    cost = 0
                    for row in range(BLOCK_SIZE):
                        first_offset = (y + row) * width + x
                        second_offset = (y + dy + row) * width + x + dx
                        cost += sum(
                            abs(first[first_offset + col] - second[second_offset + col])
                            for col in range(BLOCK_SIZE)
                        )
                    if best_cost is None or cost < best_cost:
                        second_best_cost = best_cost
                        best_cost, best_dx, best_dy = cost, dx, dy
                    elif second_best_cost is None or cost < second_best_cost:
                        second_best_cost = cost
            if (
                best_cost is not None
                and second_best_cost is not None
                and second_best_cost - best_cost >= BLOCK_SIZE * 2
            ):
                vectors.append((float(best_dx), float(best_dy), float(x), float(y)))
    return vectors

