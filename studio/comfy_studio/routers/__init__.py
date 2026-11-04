"""Registro de rutas de Comfy Studio (extraídas de app.py, M7-SCR-002)."""

from __future__ import annotations

from fastapi import FastAPI

from . import (
    ads,
    assets,
    chat,
    core,
    jobs,
    media_ops,
    montage,
    perception,
    productions,
    projects,
    templates,
)

_MODULES = (
    core,
    projects,
    productions,
    assets,
    jobs,
    templates,
    chat,
    perception,
    media_ops,
    ads,
    montage,
)


def register_all_routes(app: FastAPI) -> None:
    """Registra todas las rutas de /studio/api directamente sobre `app`.

    Las rutas se crean con los decoradores de `app`, así que `app.router.route_class`
    (`_BoundedUploadAPIRoute`) aplica igual que antes del split.
    """
    for module in _MODULES:
        module.register_routes(app)
