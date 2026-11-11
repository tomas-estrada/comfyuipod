"""Persistent project-owned RGBA layers and real CPU PNG composition (A17)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.raster_layers import (
    RasterLayer,
    RasterLayerError,
    canonical_manifest,
    composite_layers,
    parse_manifest,
)

_META_KEY = "ultra_raster_layer_set_v1"


class RasterLayerRequest(BaseModel):
    layer_id: str
    asset_id: str
    caused_by: str | None = None
    region: str = "full"
    order: int


class RasterLayerSetRequest(BaseModel):
    layers: list[RasterLayerRequest]


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def register_routes(app: FastAPI) -> None:
    @app.put("/studio/api/projects/{project_id}/raster-layers")
    async def put_raster_layers(
        project_id: str, body: RasterLayerSetRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        layers = [RasterLayer(**item.model_dump()) for item in body.layers]
        try:
            manifest = canonical_manifest(layers)
            for layer in parse_manifest(manifest):
                asset = store.get_asset(project_id, layer.asset_id)
                if asset is None:
                    raise RasterLayerError(
                        f"asset inexistente en este proyecto: {layer.asset_id}"
                    )
            store.set_meta(project_id, _META_KEY, manifest)
            return {**manifest, "render_validation": "not_run"}
        except RasterLayerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/raster-layers")
    async def get_raster_layers(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        raw = store.get_meta(project_id, _META_KEY)
        if raw is None:
            raise HTTPException(status_code=404, detail="Conjunto de capas no encontrado")
        try:
            manifest = canonical_manifest(parse_manifest(raw))
        except RasterLayerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {**manifest, "render_validation": "not_run"}

    @app.post("/studio/api/projects/{project_id}/raster-layers/composite", status_code=201)
    async def composite_raster_layers(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            layers = parse_manifest(store.get_meta(project_id, _META_KEY))
            png, evidence = composite_layers(store, project_id, layers)
            config = request.app.state.config
            asset = store.save_upload(
                project_id,
                filename="raster-layer-composite.png",
                content_type="image/png",
                data=png,
                max_upload_bytes=config.max_upload_bytes,
                category="outputs",
                metadata={
                    "artifact_kind": "raster_layer_composite",
                    "source_layers": [layer.to_dict() for layer in layers],
                    "pixel_render": "cpu_png",
                    "matting": "not_run",
                },
            )
            return {
                "asset": asset,
                "evidence": evidence,
                "editable_layers": "persisted_asset_references",
            }
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except RasterLayerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


__all__ = ["register_routes"]
