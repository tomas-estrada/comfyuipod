"""Cinematic still-image direction and prompt composition for the local Studio UI."""

from __future__ import annotations

import math
import secrets
from typing import Any

CINEMATIC_CHOICES: dict[str, tuple[str, ...]] = {
    "shot": (
        "Wide establishing shot",
        "Medium shot",
        "Close-up portrait",
        "Low-angle shot",
        "Overhead shot",
    ),
    "camera": (
        "35mm lens, shallow depth of field",
        "Wide-angle lens, deep focus",
        "85mm portrait lens, soft background",
        "Handheld documentary camera",
    ),
    "light": (
        "Soft window light",
        "Golden-hour backlight",
        "Hard noon sunlight",
        "Blue-hour practical lights",
    ),
    "look": (
        "Warm cinematic film",
        "Cool science-fiction film",
        "Natural documentary color",
        "High-contrast noir",
    ),
}

CINEMATIC_DEFAULTS: dict[str, str] = {
    "shot": "Wide establishing shot",
    "camera": "35mm lens, shallow depth of field",
    "light": "Soft window light",
    "look": "Warm cinematic film",
}

ASPECT_RATIOS: dict[str, dict[str, Any]] = {
    "16:9": {"label": "16:9 · Horizontal", "width": 1344, "height": 768},
    "9:16": {"label": "9:16 · Vertical", "width": 768, "height": 1344},
    "1:1": {"label": "1:1 · Cuadrado", "width": 1024, "height": 1024},
    "4:5": {"label": "4:5 · Retrato", "width": 832, "height": 1040},
}

DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_NEGATIVE_PROMPT = (
    "borroso, baja calidad, marca de agua, texto, manos deformadas, CGI artificial"
)
DEFAULT_STEPS = 35
DEFAULT_CFG = 5.0
DEFAULT_SEED = 42
MAX_SCENE_LENGTH = 900
MAX_NEGATIVE_PROMPT_LENGTH = 2000
MAX_SEED = 2_147_483_647


class CinematicRequestError(ValueError):
    """Invalid direction or render settings supplied to the cinematic endpoint."""


def compose_cinematic_prompt(scene: str, direction: dict[str, str]) -> str:
    """Compose a prompt in the same shot → scene → camera → light → look order."""
    scene_text = scene.strip()
    scene_separator = " " if scene_text.endswith((".", "!", "?", "…")) else ". "
    return (
        f"{direction['shot']}. {scene_text}{scene_separator}"
        f"{direction['camera']}. {direction['light']}. {direction['look']}"
    )


def cinematic_options() -> dict[str, Any]:
    """Return the option catalog and defaults consumed by the browser panel."""
    return {
        "template_id": "sdxl_image",
        "choices": {key: list(values) for key, values in CINEMATIC_CHOICES.items()},
        "aspect_ratios": [
            {"value": value, **details} for value, details in ASPECT_RATIOS.items()
        ],
        "defaults": {
            **CINEMATIC_DEFAULTS,
            "aspect_ratio": DEFAULT_ASPECT_RATIO,
            "negative_prompt": DEFAULT_NEGATIVE_PROMPT,
            "steps": DEFAULT_STEPS,
            "cfg": DEFAULT_CFG,
            "random_seed": True,
            "seed": DEFAULT_SEED,
        },
        "limits": {
            "scene_max_length": MAX_SCENE_LENGTH,
            "negative_prompt_max_length": MAX_NEGATIVE_PROMPT_LENGTH,
            "steps_min": 1,
            "steps_max": 100,
            "cfg_min": 1.0,
            "cfg_max": 20.0,
            "seed_min": 0,
            "seed_max": MAX_SEED,
        },
    }


def prepare_cinematic_render(payload: Any) -> dict[str, Any]:
    """Validate a user request and build local SDXL params plus direction metadata."""
    if not isinstance(payload, dict):
        raise CinematicRequestError("El cuerpo debe ser un objeto JSON.")

    scene_raw = payload.get("scene")
    if not isinstance(scene_raw, str):
        raise CinematicRequestError("La escena debe ser texto.")
    scene = scene_raw.strip()
    if len(scene) < 3:
        raise CinematicRequestError("La escena debe tener al menos 3 caracteres.")
    if len(scene) > MAX_SCENE_LENGTH:
        raise CinematicRequestError(
            f"La escena no puede superar {MAX_SCENE_LENGTH} caracteres."
        )

    direction: dict[str, str] = {}
    for key, choices in CINEMATIC_CHOICES.items():
        value = payload.get(key, CINEMATIC_DEFAULTS[key])
        if not isinstance(value, str) or value not in choices:
            raise CinematicRequestError(f"La opción de {key!r} no es válida.")
        direction[key] = value

    aspect_ratio = payload.get("aspect_ratio", DEFAULT_ASPECT_RATIO)
    if not isinstance(aspect_ratio, str) or aspect_ratio not in ASPECT_RATIOS:
        raise CinematicRequestError("La relación de aspecto no es válida.")
    resolution = ASPECT_RATIOS[aspect_ratio]

    negative_raw = payload.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    if not isinstance(negative_raw, str):
        raise CinematicRequestError("El prompt negativo debe ser texto.")
    negative_prompt = negative_raw.strip()
    if len(negative_prompt) > MAX_NEGATIVE_PROMPT_LENGTH:
        raise CinematicRequestError(
            "El prompt negativo no puede superar "
            f"{MAX_NEGATIVE_PROMPT_LENGTH} caracteres."
        )

    steps = payload.get("steps", DEFAULT_STEPS)
    if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 100:
        raise CinematicRequestError("Los pasos deben ser un entero entre 1 y 100.")

    cfg = payload.get("cfg", DEFAULT_CFG)
    if isinstance(cfg, bool) or not isinstance(cfg, (int, float)):
        raise CinematicRequestError("CFG debe ser un número entre 1 y 20.")
    cfg = float(cfg)
    if not math.isfinite(cfg) or not 1.0 <= cfg <= 20.0:
        raise CinematicRequestError("CFG debe ser un número entre 1 y 20.")

    random_seed = payload.get("random_seed", True)
    if not isinstance(random_seed, bool):
        raise CinematicRequestError("random_seed debe ser true o false.")
    if random_seed:
        seed = secrets.randbelow(MAX_SEED + 1)
    else:
        seed = payload.get("seed", DEFAULT_SEED)
        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
            raise CinematicRequestError(f"La semilla debe ser un entero entre 0 y {MAX_SEED}.")

    prompt = compose_cinematic_prompt(scene, direction)
    params = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": resolution["width"],
        "height": resolution["height"],
        "steps": steps,
        "cfg": cfg,
        "seed": seed,
    }
    return {
        "scene": scene,
        "direction": direction,
        "aspect_ratio": aspect_ratio,
        "params": params,
    }
