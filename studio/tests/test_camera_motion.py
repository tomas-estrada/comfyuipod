"""Synthetic CPU contracts for the conservative local camera-motion estimator."""

from __future__ import annotations

import random

from comfy_studio.perception.camera_motion import estimate_camera_motion

WIDTH = 64
HEIGHT = 36


def _texture(seed: int = 7) -> bytes:
    return random.Random(seed).randbytes(WIDTH * HEIGHT)


def _patch_texture(seed: int, width: int, height: int) -> bytes:
    return random.Random(seed).randbytes(width * height)


def _compose(
    background: bytes,
    patch: bytes,
    patch_x: int,
    patch_y: int,
    patch_w: int,
    patch_h: int,
) -> bytes:
    """Overlay a patch onto a background frame at a fixed position."""
    result = bytearray(background)
    for row in range(patch_h):
        for col in range(patch_w):
            result[(patch_y + row) * WIDTH + patch_x + col] = patch[row * patch_w + col]
    return bytes(result)


def _subject_clip(
    background: bytes,
    subject: bytes,
    start_x: int,
    step_x: int,
    frame_count: int,
    subject_w: int,
    subject_h: int,
) -> list[bytes]:
    """Deterministic clip: static background, subject translating horizontally."""
    return [
        _compose(background, subject, start_x + step_x * index, 9, subject_w, subject_h)
        for index in range(frame_count)
    ]


def _shift(frame: bytes, dx: int, dy: int) -> bytes:
    result = bytearray(WIDTH * HEIGHT)
    for y in range(HEIGHT):
        for x in range(WIDTH):
            sx, sy = x - dx, y - dy
            if 0 <= sx < WIDTH and 0 <= sy < HEIGHT:
                result[y * WIDTH + x] = frame[sy * WIDTH + sx]
    return bytes(result)


def _zoom_in(frame: bytes, scale: float) -> bytes:
    result = bytearray(WIDTH * HEIGHT)
    center_x, center_y = (WIDTH - 1) / 2, (HEIGHT - 1) / 2
    for y in range(HEIGHT):
        for x in range(WIDTH):
            sx = round(center_x + (x - center_x) / scale)
            sy = round(center_y + (y - center_y) / scale)
            sx = min(WIDTH - 1, max(0, sx))
            sy = min(HEIGHT - 1, max(0, sy))
            result[y * WIDTH + x] = frame[sy * WIDTH + sx]
    return bytes(result)


def test_textured_identical_frames_are_static() -> None:
    frame = _texture()

    estimate = estimate_camera_motion([frame, frame], WIDTH, HEIGHT)

    assert estimate.move == "static"
    assert estimate.confidence >= 0.6


def test_coherent_global_translation_is_pan() -> None:
    frame = _texture()

    estimate = estimate_camera_motion([frame, _shift(frame, 2, -1)], WIDTH, HEIGHT)

    assert estimate.move == "pan"
    assert estimate.confidence >= 0.6


def test_moving_foreground_patch_does_not_become_camera_pan() -> None:
    frame = _texture()
    changed = bytearray(frame)
    for y in range(8, 24):
        for x in range(8, 24):
            changed[y * WIDTH + x] = 255 - changed[y * WIDTH + x]

    estimate = estimate_camera_motion([frame, bytes(changed)], WIDTH, HEIGHT)

    assert estimate.move == "static"


def test_radial_global_motion_is_zoom() -> None:
    frame = _texture()

    estimate = estimate_camera_motion([frame, _zoom_in(frame, 1.12)], WIDTH, HEIGHT)

    assert estimate.move == "zoom"
    assert estimate.confidence >= 0.5


def test_textureless_or_malformed_frames_remain_unknown() -> None:
    flat = bytes([127]) * (WIDTH * HEIGHT)

    flat_estimate = estimate_camera_motion([flat, flat], WIDTH, HEIGHT)
    malformed_estimate = estimate_camera_motion([b"short", flat], WIDTH, HEIGHT)

    assert flat_estimate.move == "unknown"
    assert malformed_estimate.move == "unknown"


def test_single_frame_is_unknown() -> None:
    estimate = estimate_camera_motion([_texture()], WIDTH, HEIGHT)

    assert estimate.move == "unknown"


def test_synthetic_clip_static_camera_with_moving_subject_is_not_camera_move() -> None:
    """Ground truth: camera static, textured subject translating across frame."""
    background = _texture(11)
    subject = _patch_texture(12, 16, 22)
    frames = _subject_clip(background, subject, 20, -2, 5, 16, 22)

    estimate = estimate_camera_motion(frames, WIDTH, HEIGHT)

    assert estimate.move in {"static", "unknown"}
    assert estimate.to_dict()["origin"] != "inferred" or estimate.move == "static"


def test_synthetic_clip_flat_background_subject_motion_stays_unknown() -> None:
    """Ground truth: flat background, only the subject carries motion evidence.

    A subject-sized field cannot confirm a camera move, so the estimator must
    report uncertainty instead of a confident pan.
    """
    background = bytes([127]) * (WIDTH * HEIGHT)
    subject = _patch_texture(12, 16, 22)
    frames = _subject_clip(background, subject, 20, -2, 5, 16, 22)

    estimate = estimate_camera_motion(frames, WIDTH, HEIGHT)

    assert estimate.move == "unknown"
    assert estimate.confidence <= 0.5


def test_synthetic_clip_camera_pan_is_detected_across_frames() -> None:
    """Ground truth: whole frame translating (2, -1) per pair, no subject."""
    base = _texture(11)
    frames = [_shift(base, 2 * index, -index) for index in range(5)]

    estimate = estimate_camera_motion(frames, WIDTH, HEIGHT)

    assert estimate.move == "pan"
    assert estimate.confidence >= 0.6


def test_synthetic_clip_pan_with_counter_moving_subject_still_reports_pan() -> None:
    """Ground truth: camera pans while a smaller subject moves the other way.

    Background evidence dominates spatially, so the camera move must win.
    """
    base = _texture(11)
    subject = _patch_texture(12, 16, 22)
    frames = [
        _compose(_shift(base, 2 * index, -index), subject, 20 - 2 * index, 9, 16, 22)
        for index in range(5)
    ]

    estimate = estimate_camera_motion(frames, WIDTH, HEIGHT)

    assert estimate.move == "pan"
    assert estimate.confidence >= 0.6


def test_synthetic_clip_partially_textured_pan_stays_conservative() -> None:
    """Ground truth: camera pans, but texture (and thus evidence) is lower-half only.

    Evidence covers half the grid; the estimator refuses a confident camera
    label from a partial field and reports unknown instead.
    """
    base = _texture(11)
    mixed = bytearray(bytes([127]) * (WIDTH * HEIGHT))
    for y in range(HEIGHT // 2, HEIGHT):
        for x in range(WIDTH):
            mixed[y * WIDTH + x] = base[y * WIDTH + x]
    frames = [_shift(bytes(mixed), 2 * index, -index) for index in range(5)]

    estimate = estimate_camera_motion(frames, WIDTH, HEIGHT)

    assert estimate.move == "unknown"
    assert estimate.confidence <= 0.5
