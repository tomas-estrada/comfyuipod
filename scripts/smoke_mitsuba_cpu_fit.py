#!/usr/bin/env python3
"""Fit a known Mitsuba scene parameter on CPU and evaluate it on held-out view."""

from __future__ import annotations

import json
import math

import drjit as dr
import mitsuba as mi

WIDTH = 32
HEIGHT = 32
TARGET_RED = 0.70
INITIAL_RED = 0.10
TRAIN_SEED = 31
HELDOUT_SEED = 97


def _camera(origin: tuple[float, float, float]) -> mi.ScalarTransform4f:
    return mi.ScalarTransform4f().look_at(
        mi.ScalarPoint3f(*origin),
        mi.ScalarPoint3f(0, 0, 0),
        mi.ScalarPoint3f(0, 1, 0),
    )


def _mse(image, reference) -> float:
    return float(dr.mean(dr.square(image - reference)).array[0])


def main() -> int:
    mi.set_variant("scalar_rgb")
    training_camera = _camera((0.0, 0.0, 4.0))
    heldout_camera = _camera((2.5, -1.0, 3.0))
    scene = mi.load_dict(
        {
            "type": "scene",
            "integrator": {"type": "path", "max_depth": 3},
            "sensor": {
                "type": "perspective",
                "fov": 40,
                "to_world": training_camera,
                "sampler": {"type": "independent", "sample_count": 16},
                "film": {
                    "type": "hdrfilm",
                    "width": WIDTH,
                    "height": HEIGHT,
                    "rfilter": {"type": "box"},
                },
            },
            "sphere": {
                "type": "sphere",
                "bsdf": {
                    "type": "diffuse",
                    "reflectance": {
                        "type": "rgb",
                        "value": [INITIAL_RED, 0.2, 0.1],
                    },
                },
            },
            "environment": {
                "type": "constant",
                "radiance": {"type": "rgb", "value": [3.0, 3.0, 3.0]},
            },
        }
    )
    params = mi.traverse(scene)
    material_key = "sphere.bsdf.reflectance.value"
    camera_key = "sensor.to_world"
    target = mi.Color3f(TARGET_RED, 0.2, 0.1)

    params[material_key] = target
    params[camera_key] = training_camera
    params.update()
    training_reference = mi.render(scene, params=params, spp=64, seed=TRAIN_SEED)
    params[camera_key] = heldout_camera
    params.update()
    heldout_reference = mi.render(scene, params=params, spp=128, seed=HELDOUT_SEED)

    # The optimizer sees only the training-view image objective, not TARGET_RED.
    def objective(red: float) -> float:
        params[material_key] = mi.Color3f(red, 0.2, 0.1)
        params[camera_key] = training_camera
        params.update()
        image = mi.render(scene, params=params, spp=64, seed=TRAIN_SEED)
        return _mse(image, training_reference)

    params[material_key] = mi.Color3f(INITIAL_RED, 0.2, 0.1)
    params[camera_key] = heldout_camera
    params.update()
    baseline_heldout = mi.render(scene, params=params, spp=128, seed=HELDOUT_SEED)
    baseline_heldout_mse = _mse(baseline_heldout, heldout_reference)
    baseline_parameter_mse = (INITIAL_RED - TARGET_RED) ** 2

    # Bounded one-dimensional golden-section fit of a physically interpretable
    # diffuse albedo. This black-box CPU fit intentionally avoids requiring LLVM.
    low, high = 0.001, 0.999
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    left = high - ratio * (high - low)
    right = low + ratio * (high - low)
    left_loss, right_loss = objective(left), objective(right)
    iterations = 24
    for _ in range(iterations):
        if left_loss < right_loss:
            high, right, right_loss = right, left, left_loss
            left = high - ratio * (high - low)
            left_loss = objective(left)
        else:
            low, left, left_loss = left, right, right_loss
            right = low + ratio * (high - low)
            right_loss = objective(right)
    recovered_red = (low + high) / 2.0
    params[material_key] = mi.Color3f(recovered_red, 0.2, 0.1)
    params[camera_key] = heldout_camera
    params.update()
    fitted_heldout = mi.render(scene, params=params, spp=128, seed=HELDOUT_SEED)

    fitted_parameter_mse = (recovered_red - TARGET_RED) ** 2
    fitted_heldout_mse = _mse(fitted_heldout, heldout_reference)
    report = {
        "engine": "mitsuba",
        "version": mi.__version__,
        "variant": "scalar_rgb",
        "device": "cpu",
        "gpu_used": False,
        "fixture": {"width": WIDTH, "height": HEIGHT, "target_parameter": "diffuse red albedo"},
        "baseline": {
            "parameter_mse": baseline_parameter_mse,
            "heldout_image_mse": baseline_heldout_mse,
        },
        "fit": {
            "iterations": iterations,
            "recovered_red_albedo": recovered_red,
            "parameter_mse": fitted_parameter_mse,
            "heldout_image_mse": fitted_heldout_mse,
        },
        "ground_truth_red_albedo": TARGET_RED,
        "train_seed": TRAIN_SEED,
        "heldout_seed": HELDOUT_SEED,
        "heldout_camera": "different viewpoint from training camera",
    }
    if fitted_parameter_mse > 0.0004:
        raise RuntimeError(f"Mitsuba CPU fit did not recover known albedo: {report}")
    if baseline_parameter_mse <= fitted_parameter_mse:
        raise RuntimeError(f"Mitsuba fit did not beat the parameter baseline: {report}")
    if baseline_heldout_mse <= fitted_heldout_mse:
        raise RuntimeError(f"Mitsuba fit did not improve held-out view error: {report}")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
