"""Deterministic geometry-based staging candidates and RGBA render scoring.

This is a small CPU-side heuristic for static, whole-asset product staging. It
does not inspect semantics, optimize a differentiable objective, or use a GPU.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

_MAX_CANDIDATES = 8
_MAX_EXTENT = 1_000_000.0


def _aabb_bounds(
    aabb: Mapping[str, Any],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not isinstance(aabb, Mapping):
        raise ValueError("aabb must be a mapping with min and max vectors")
    try:
        low = tuple(float(v) for v in aabb["min"])
        high = tuple(float(v) for v in aabb["max"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("aabb min/max must be finite 3D vectors") from exc
    if len(low) != 3 or len(high) != 3 or not all(math.isfinite(v) for v in low + high):
        raise ValueError("aabb min/max must be finite 3D vectors")
    extents = tuple(high[i] - low[i] for i in range(3))
    if any(size < 0 or size > _MAX_EXTENT for size in extents) or not any(extents):
        raise ValueError(
            "aabb extents must be non-negative, non-degenerate and at most 1,000,000 units"
        )
    return low, high


def generate_staging_candidates(
    aabb: Mapping[str, Any], up_axis: str, *, max_candidates: int = _MAX_CANDIDATES
) -> list[dict[str, Any]]:
    """Return up to eight reproducible camera/light setups for an AABB.

    Geometry assumptions: the asset is static; its supplied AABB encloses the
    entire asset; its center is the look-at target; the camera views the box's
    bounding sphere from 35 degrees above the horizontal; and lights are
    positioned relative to that center. No surface or semantic information is
    inferred. The fixed 4 azimuths × 2 key sides vary both requested factors.
    """
    low, high = _aabb_bounds(aabb)
    if up_axis not in ("y_up", "z_up"):
        raise ValueError("up_axis must be 'y_up' or 'z_up'")
    if (
        isinstance(max_candidates, bool)
        or not isinstance(max_candidates, int)
        or not 1 <= max_candidates <= _MAX_CANDIDATES
    ):
        raise ValueError("max_candidates must be an integer from 1 to 8")
    center = tuple((low[i] + high[i]) / 2 for i in range(3))
    radius = math.sqrt(sum(((high[i] - low[i]) / 2) ** 2 for i in range(3)))
    fov = 40.0
    distance = radius / math.tan(math.radians(fov / 2)) * 1.12
    # Azimuth is around the declared up axis. Four views are front/right/back/left.
    horizontal = (0, 2) if up_axis == "y_up" else (0, 1)
    vertical = 1 if up_axis == "y_up" else 2
    vertical_offset = distance * math.sin(math.radians(35))
    horizontal_distance = distance * math.cos(math.radians(35))
    candidates = []
    for azimuth in (0, 90, 180, 270):
        angle = math.radians(azimuth)
        for key_side in ("left", "right"):
            x = math.cos(angle) * horizontal_distance
            z = math.sin(angle) * horizontal_distance
            camera = [center[0], center[1], center[2]]
            camera[horizontal[0]] += x
            camera[horizontal[1]] += z
            camera[vertical] += vertical_offset
            side_sign = -1.0 if key_side == "left" else 1.0
            key = [center[0], center[1], center[2]]
            key[horizontal[0]] += side_sign * radius * 1.5
            key[horizontal[1]] -= radius * 1.2
            key[vertical] += radius * 2.0
            fill = [center[0], center[1], center[2]]
            fill[horizontal[0]] -= side_sign * radius * 1.25
            fill[horizontal[1]] += radius * 0.8
            fill[vertical] += radius * 0.5
            candidates.append(
                {
                    "candidate_id": f"az{azimuth:03d}_{key_side}",
                    "azimuth_degrees": azimuth,
                    "key_light_side": key_side,
                    "camera_position": camera,
                    "camera_target": list(center),
                    "camera_fov_degrees": fov,
                    "light_rig": {
                        "key": {
                            "position": key,
                            "intensity": 1.0,
                            "size": min(radius * 1.5, 100_000.0),
                        },
                        "fill": {
                            "position": fill,
                            "intensity": 0.35,
                            "size": min(radius * 2.0, 100_000.0),
                        },
                        "ambient_intensity": 0.12,
                    },
                    "assumptions": (
                        "Static whole asset; supplied AABB encloses asset; target is AABB center; "
                        "camera uses bounding sphere and fixed 35-degree elevation; lights are "
                        "geometry-relative."
                    ),
                }
            )
            if len(candidates) == max_candidates:
                return candidates
    return candidates


def score_rgba_render(rgba: Any) -> dict[str, float]:
    """Score an H×W×4 array-like RGBA image using transparent-object heuristics.

    Values are interpreted in [0, 1]. Rejects empty, malformed, non-finite,
    out-of-range, or fully transparent renders. The score is a bounded visual
    staging heuristic, not a semantic-salience judgment.
    """
    try:
        height = len(rgba)
        width = len(rgba[0]) if height else 0
        if height == 0 or width == 0:
            raise ValueError("render is empty")
        if height > 16_384 or width > 16_384 or height * width > 100_000_000:
            raise ValueError("render dimensions exceed supported bounds")
        visible_count = 0
        centroid_x_sum = 0.0
        centroid_y_sum = 0.0
        luminance_sum = 0.0
        luminance_sq_sum = 0.0
        for y, row in enumerate(rgba):
            if len(row) != width:
                raise ValueError("render rows must have equal width")
            for x, pixel in enumerate(row):
                if len(pixel) != 4:
                    raise ValueError("render must contain RGBA pixels")
                values = tuple(float(v) for v in pixel)
                if not all(math.isfinite(v) and 0 <= v <= 1 for v in values):
                    raise ValueError("RGBA components must be finite and in [0, 1]")
                if values[3] > 0.01:
                    luminance = 0.2126 * values[0] + 0.7152 * values[1] + 0.0722 * values[2]
                    visible_count += 1
                    centroid_x_sum += x
                    centroid_y_sum += y
                    luminance_sum += luminance
                    luminance_sq_sum += luminance * luminance
    except (TypeError, IndexError) as exc:
        raise ValueError("render must be a non-empty H×W×4 array-like value") from exc
    if not visible_count:
        raise ValueError("render contains no visible object pixels")
    count = height * width
    coverage = visible_count / count
    centroid_x = centroid_x_sum / visible_count
    centroid_y = centroid_y_sum / visible_count
    normalized_x = (centroid_x - (width - 1) / 2) / max(width / 2, 1)
    normalized_y = (centroid_y - (height - 1) / 2) / max(height / 2, 1)
    centeredness = max(0.0, 1.0 - math.sqrt(normalized_x**2 + normalized_y**2) / math.sqrt(2))
    mean = luminance_sum / visible_count
    variance = max(0.0, luminance_sq_sum / visible_count - mean * mean)
    contrast = math.sqrt(variance)
    # Broad exposure preference plus useful object occupancy, centering and contrast.
    exposure = max(0.0, 1.0 - abs(mean - 0.48) / 0.52)
    occupancy = max(0.0, 1.0 - abs(coverage - 0.42) / 0.58)
    contrast_quality = min(1.0, contrast / 0.25)
    score = max(
        0.0,
        min(
            1.0,
            0.35 * occupancy + 0.30 * centeredness + 0.20 * exposure + 0.15 * contrast_quality,
        ),
    )
    return {
        "transparent_object_coverage": coverage,
        "centering": centeredness,
        "luminance_mean": mean,
        "luminance_contrast": contrast,
        "score": score,
    }
