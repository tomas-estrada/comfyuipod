"""Registro de rutas de Comfy Studio (extraídas de app.py, M7-SCR-002)."""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    ads,
    assets,
    assets3d,
    atlas,
    audio,
    brief,
    chat,
    cinematic,
    core,
    jobs,
    media_ops,
    montage,
    motion,
    perception,
    productions,
    projects,
    raster_layers,
    relight,
    review_pins,
    spatial_memory,
    templates,
    voice,
)

_MODULES = (
    core,
    atlas,
    projects,
    productions,
    assets,
    assets3d,
    jobs,
    templates,
    cinematic,
    chat,
    perception,
    media_ops,
    brief,
    ads,
    audio,
    review_pins,
    spatial_memory,
    montage,
    motion,
    relight,
    raster_layers,
    voice,
)


def register_all_routes(app: FastAPI) -> None:
    """Registra todas las rutas de /studio/api directamente sobre `app`.

    Las rutas se crean con los decoradores de `app`, así que `app.router.route_class`
    (`_BoundedUploadAPIRoute`) aplica igual que antes del split.
    """
    for module in _MODULES:
        module.register_routes(app)
