"""Persistent project-owned RGBA layers and real CPU PNG composition (A17)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.raster_layers import (
    RasterLayer,
    RasterLayerError,
    affected_by,
    canonical_manifest,
    composite_layers,
    parse_manifest,
)

_META_KEY = "ultra_raster_layer_set_v1"
_OUTPUT_META_KEY = "ultra_raster_layer_output_v1"


class RasterLayerRequest(BaseModel):
    layer_id: str
    asset_id: str
    caused_by: str | None = None
    region: str = "full"
    order: int


class RasterLayerSetRequest(BaseModel):
    layers: list[RasterLayerRequest]


class RasterLayerEditRequest(BaseModel):
    asset_id: str


def _mark_previous_output_stale(
    store: ProjectStore, project_id: str, invalidated: set[str]
) -> None:
    previous_output = store.get_meta(project_id, _OUTPUT_META_KEY)
    if not isinstance(previous_output, dict) or not previous_output.get("asset_id"):
        return
    store.update_asset_metadata(
        project_id,
        str(previous_output["asset_id"]),
        metadata={
            "stale": True,
            "stale_reason": "raster_layer_source_changed",
            "invalidated_layer_ids": sorted(invalidated),
        },
    )
    store.set_meta(
        project_id,
        _OUTPUT_META_KEY,
        {**previous_output, "status": "stale"},
    )


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
            previous = store.get_meta(project_id, _META_KEY)
            old_layers = parse_manifest(previous) if previous is not None else []
            old_by_id = {layer.layer_id: layer for layer in old_layers}
            new_by_id = {layer.layer_id: layer for layer in layers}
            invalidated = {
                layer_id
                for layer_id in old_by_id.keys() | new_by_id.keys()
                if old_by_id.get(layer_id) != new_by_id.get(layer_id)
            }
            if invalidated:
                descendants = set(invalidated)
                for changed_id in tuple(invalidated):
                    if changed_id in old_by_id:
                        descendants.update(affected_by(old_layers, changed_id))
                    if changed_id in new_by_id:
                        descendants.update(affected_by(layers, changed_id))
                _mark_previous_output_stale(store, project_id, descendants)
                invalidated = descendants
            store.set_meta(project_id, _META_KEY, manifest)
            return {
                **manifest,
                "render_validation": "not_run",
                "invalidated_layer_ids": sorted(invalidated),
            }
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

    @app.patch("/studio/api/projects/{project_id}/raster-layers/{layer_id}")
    async def edit_raster_layer(
        project_id: str,
        layer_id: str,
        body: RasterLayerEditRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            layers = parse_manifest(store.get_meta(project_id, _META_KEY))
            asset = store.get_asset(project_id, body.asset_id)
            if asset is None or asset.get("kind") != "image":
                raise RasterLayerError("el nuevo origen debe ser una imagen del proyecto")
            affected = affected_by(layers, layer_id)
            edited = [
                replace(layer, asset_id=body.asset_id)
                if layer.layer_id == layer_id
                else layer
                for layer in layers
            ]
            manifest = canonical_manifest(edited)
            store.set_meta(project_id, _META_KEY, manifest)
            _mark_previous_output_stale(store, project_id, affected)
            return {
                **manifest,
                "invalidated_layer_ids": sorted(affected),
                "recomposition": "required_not_run",
            }
        except RasterLayerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            store.set_meta(
                project_id,
                _OUTPUT_META_KEY,
                {
                    "asset_id": asset["id"],
                    "sha256": evidence["sha256"],
                    "status": "current",
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
