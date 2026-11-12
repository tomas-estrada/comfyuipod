"""Synthetic CPU contracts for the conservative local camera-motion estimator."""

from __future__ import annotations

import random

from comfy_studio.perception.camera_motion import estimate_camera_motion

WIDTH = 64
HEIGHT = 36


def _texture(seed: int = 7) -> bytes:
    return random.Random(seed).randbytes(WIDTH * HEIGHT)


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
