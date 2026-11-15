"""Bounded, CPU-only image-plane observations from project-owned video.

This deliberately stops short of semantic object detection or 3D recovery. It
records the centroid and bounds of changed pixels between decoded, sampled
frames. The resulting temporal record is useful for editing/inspection while
depth, articulated pose and camera pose remain explicitly unknown.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops

from .media_bins import find_ffmpeg, find_ffprobe
from .spatial_memory import Provenance, SourceRef, TemporalScene, Timebase

OBSERVATION_SCHEMA = "video-temporal-observation/1"
SAMPLE_WIDTH = 160
SAMPLE_HEIGHT = 90
MAX_DURATION_S = 120.0
MAX_SAMPLE_FPS = 10
MAX_SAMPLED_FRAMES = 128
MAX_SOURCE_BYTES = 512 * 1024 * 1024
PIXEL_CHANGE_THRESHOLD = 24
MIN_CHANGED_PIXELS = 8


class VideoSceneError(ValueError):
    """Video cannot be converted within the bounded observation contract."""


def _validate_sha256(value: str) -> str:
    value = value.lower() if isinstance(value, str) else ""
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise VideoSceneError("source_sha256 must be a 64-character hex digest")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def analyze_grayscale_frames(
    frames: Sequence[Image.Image],
    *,
    source_asset_id: str,
    source_sha256: str,
    duration_s: float,
    sample_fps: int,
    source_timebase: str,
) -> dict[str, Any]:
    """Make a source-bound sequence of normalized frame-change observations."""
    if not isinstance(source_asset_id, str) or not source_asset_id.strip():
        raise VideoSceneError("source_asset_id is required")
    digest = _validate_sha256(source_sha256)
    if (
        isinstance(sample_fps, bool)
        or not isinstance(sample_fps, int)
        or not 1 <= sample_fps <= MAX_SAMPLE_FPS
    ):
        raise VideoSceneError(f"sample_fps must be an integer from 1 to {MAX_SAMPLE_FPS}")
    try:
        duration = float(duration_s)
    except (TypeError, ValueError) as exc:
        raise VideoSceneError("video duration must be finite") from exc
    if (
        isinstance(duration_s, bool)
        or not math.isfinite(duration)
        or not 0 < duration <= MAX_DURATION_S
    ):
        raise VideoSceneError(f"video duration must be in (0, {MAX_DURATION_S}] seconds")
    if len(frames) < 1 or len(frames) > MAX_SAMPLED_FRAMES:
        raise VideoSceneError(f"sampled frame count must be from 1 to {MAX_SAMPLED_FRAMES}")
    if len(frames) > 1 and len(frames) > duration * sample_fps + 1:
        raise VideoSceneError("sampled frames exceed the declared duration and timebase")
    if not isinstance(source_timebase, str) or not source_timebase.strip():
        raise VideoSceneError("source_timebase is required")

    keys: list[dict[str, Any]] = []
    transitions = 0
    for index, (previous, current) in enumerate(
        zip(frames[:-1], frames[1:], strict=True), start=1
    ):
        previous_gray = previous.convert("L").resize((SAMPLE_WIDTH, SAMPLE_HEIGHT))
        current_gray = current.convert("L").resize((SAMPLE_WIDTH, SAMPLE_HEIGHT))
        diff = ImageChops.difference(previous_gray, current_gray)
        pixels = diff.load()
        changed = [
            (x, y)
            for y in range(SAMPLE_HEIGHT)
            for x in range(SAMPLE_WIDTH)
            if pixels[x, y] >= PIXEL_CHANGE_THRESHOLD
        ]
        if len(changed) < MIN_CHANGED_PIXELS:
            continue
        transitions += 1
        xs = [point[0] for point in changed]
        ys = [point[1] for point in changed]
        left, right = min(xs), max(xs) + 1
        top, bottom = min(ys), max(ys) + 1
        keys.append(
            {
                "t": round(min(index / sample_fps, duration), 6),
                "bbox": [
                    round(left / SAMPLE_WIDTH, 6),
                    round(top / SAMPLE_HEIGHT, 6),
                    round(right / SAMPLE_WIDTH, 6),
                    round(bottom / SAMPLE_HEIGHT, 6),
                ],
                "center": [
                    round(sum(xs) / len(xs) / SAMPLE_WIDTH, 6),
                    round(sum(ys) / len(ys) / SAMPLE_HEIGHT, 6),
                ],
                "changed_fraction": round(len(changed) / (SAMPLE_WIDTH * SAMPLE_HEIGHT), 6),
                "changed_pixels": len(changed),
            }
        )

    tracks = (
        [
            {
                "id": "frame_change_1",
                "kind": "frame_difference_region",
                "semantic_identity": "unknown",
                "coordinate_space": "normalized_image_plane",
                "keys": keys,
            }
        ]
        if keys
        else []
    )
    # Keep the observation directly editable through the existing temporal
    # scene model. Its z=0 is a display-plane convention, never recovered depth.
    editable_scene = TemporalScene(
        timebase=Timebase(sample_fps),
        duration_s=duration,
        max_entities=1,
        max_keys_per_entity=MAX_SAMPLED_FRAMES,
    )
    editable_scene.add_source(
        source_asset_id, SourceRef(kind="observed_video", sha256=digest)
    )
    if keys:
        for key in keys:
            editable_scene.set_object_key(
                "frame_change_1",
                key["t"],
                (key["center"][0], key["center"][1], 0.0),
                provenance=Provenance.OBSERVED,
                source_id=source_asset_id,
            )
    return {
        "schema": OBSERVATION_SCHEMA,
        "source": {
            "asset_id": source_asset_id,
            "kind": "project_video",
            "sha256": digest,
        },
        "duration_s": round(duration, 6),
        "timebase": {
            "sample_fps": sample_fps,
            "source_video_timebase": source_timebase,
            "timestamps": "seconds_from_video_start",
        },
        "image": {"sample_width": SAMPLE_WIDTH, "sample_height": SAMPLE_HEIGHT},
        "analysis": {
            "method": "consecutive_frame_grayscale_difference",
            "pixel_threshold": PIXEL_CHANGE_THRESHOLD,
            "minimum_changed_pixels": MIN_CHANGED_PIXELS,
            "sampled_frames": len(frames),
            "transitions_with_changes": transitions,
        },
        "tracks": tracks,
        "temporal_scene_coordinate_space": "normalized_image_plane; z=0 is a display convention",
        "temporal_scene": editable_scene.to_dict(),
        "unknowns": {
            "semantic_identity": "unknown",
            "depth": "unknown",
            "camera_motion": "unknown",
            "camera_pose": "unknown",
            "articulated_pose": "unknown",
        },
        "limitations": [
            "Regions are thresholded pixel changes, not semantic objects or persistent identities.",
            "Camera motion and illumination changes can also create frame differences.",
            "Depth, metric scale, camera calibration, and articulated pose are not recovered.",
        ],
    }


def _run(command: list[str], *, timeout_s: float) -> bytes:
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VideoSceneError(f"video tool failed: {exc}") from exc
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise VideoSceneError(f"video tool exited {result.returncode}: {message}")
    return result.stdout


def observe_video(
    path: Path,
    *,
    source_asset_id: str,
    sample_fps: int = 2,
    ffmpeg_bin: str | None = None,
    ffprobe_bin: str | None = None,
) -> dict[str, Any]:
    """Decode a project video locally and return a bounded image-plane record."""
    path = Path(path)
    if not path.is_file():
        raise VideoSceneError("project video asset is unavailable")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise VideoSceneError(f"video exceeds {MAX_SOURCE_BYTES} byte CPU limit")
    source_digest = _sha256_file(path)
    if (
        isinstance(sample_fps, bool)
        or not isinstance(sample_fps, int)
        or not 1 <= sample_fps <= MAX_SAMPLE_FPS
    ):
        raise VideoSceneError(f"sample_fps must be an integer from 1 to {MAX_SAMPLE_FPS}")
    ffmpeg = ffmpeg_bin or find_ffmpeg()
    ffprobe = ffprobe_bin or find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise VideoSceneError("ffmpeg and ffprobe are required for local video observation")

    probe_bytes = _run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=time_base,avg_frame_rate,r_frame_rate:format=duration", "-of", "json", str(path)],
        timeout_s=15,
    )
    try:
        probe = json.loads(probe_bytes)
        stream = probe["streams"][0]
        duration_s = float(probe["format"]["duration"])
        source_timebase = str(stream["time_base"])
        source_frame_rate = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise VideoSceneError("ffprobe did not return a video stream and duration") from exc
    if not math.isfinite(duration_s) or not 0 < duration_s <= MAX_DURATION_S:
        raise VideoSceneError(f"video duration must be in (0, {MAX_DURATION_S}] seconds")
    expected_frames = min(MAX_SAMPLED_FRAMES, math.ceil(duration_s * sample_fps) + 1)
    if math.ceil(duration_s * sample_fps) > MAX_SAMPLED_FRAMES:
        raise VideoSceneError("video duration at requested sample rate exceeds 128 frames")

    raw = _run(
        [ffmpeg, "-nostdin", "-v", "error", "-i", str(path), "-map", "0:v:0",
         "-vf", (f"fps={sample_fps},scale={SAMPLE_WIDTH}:{SAMPLE_HEIGHT}:"
                 "force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2,format=gray"),
         "-frames:v", str(expected_frames), "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        timeout_s=max(30.0, duration_s * 3.0),
    )
    frame_bytes = SAMPLE_WIDTH * SAMPLE_HEIGHT
    if len(raw) % frame_bytes:
        raise VideoSceneError("ffmpeg returned an incomplete decoded frame")
    frame_count = len(raw) // frame_bytes
    if frame_count < 1 or frame_count > MAX_SAMPLED_FRAMES:
        raise VideoSceneError("ffmpeg returned no frames or exceeded the frame limit")
    frames = [
        Image.frombytes(
            "L",
            (SAMPLE_WIDTH, SAMPLE_HEIGHT),
            raw[i * frame_bytes:(i + 1) * frame_bytes],
        )
        for i in range(frame_count)
    ]
    if _sha256_file(path) != source_digest:
        raise VideoSceneError("project video changed while it was being decoded")
    artifact = analyze_grayscale_frames(
        frames,
        source_asset_id=source_asset_id,
        source_sha256=source_digest,
        duration_s=duration_s,
        sample_fps=sample_fps,
        source_timebase=source_timebase,
    )
    artifact["timebase"]["source_frame_rate"] = source_frame_rate
    return artifact
