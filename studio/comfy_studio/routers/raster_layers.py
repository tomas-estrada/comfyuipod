"""Persistent project-owned RGBA layers and real CPU PNG composition (A17)."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.raster_layers import (
    RasterLayer,
    RasterLayerError,
    affected_by,
    canonical_manifest,
    composite_layers,
    parse_manifest,
    validate_asset_references,
)
from comfy_studio.video_matting import (
    DEFAULT_BLEND,
    DEFAULT_SIMILARITY,
    ChromaKeyError,
    chroma_key_video,
)

_META_KEY = "ultra_raster_layer_set_v1"
_OUTPUT_META_KEY = "ultra_raster_layer_output_v1"
_CHROMA_OUTPUT_META_KEY = "ultra_chroma_key_output_v1"


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


class ChromaKeyRequest(BaseModel):
    """Chroma-key declarado: UN color plano elegido explícitamente por el usuario."""

    asset_id: str
    key_color: str | list[int]
    similarity: float = DEFAULT_SIMILARITY
    blend: float = DEFAULT_BLEND


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


def _mark_previous_chroma_output_stale(
    store: ProjectStore, project_id: str, current: dict[str, Any]
) -> list[str]:
    """Marca los assets de la ejecución previa de chroma-key como obsoletos.

    Sigue el patrón de ``_mark_previous_output_stale``: si la fuente (asset o
    sha256) o los parámetros declarados cambian entre ejecuciones, la salida y
    el frame previos dejan de ser frescos. Devuelve los ids invalidados.
    """
    previous = store.get_meta(project_id, _CHROMA_OUTPUT_META_KEY)
    if not isinstance(previous, dict) or not previous.get("asset_id"):
        return []
    tracked = ("source_asset_id", "source_sha256", "key_color", "similarity", "blend")
    changed = [field for field in tracked if previous.get(field) != current.get(field)]
    if not changed:
        return []
    stale_metadata = {
        "stale": True,
        "stale_reason": "chroma_key_input_changed",
        "chroma_key_changed_fields": sorted(changed),
    }
    invalidated: list[str] = []
    for asset_id in (previous.get("asset_id"), previous.get("frame_asset_id")):
        if not asset_id:
            continue
        updated = store.update_asset_metadata(
            project_id, str(asset_id), metadata=stale_metadata
        )
        if updated is not None:
            invalidated.append(str(asset_id))
    store.set_meta(project_id, _CHROMA_OUTPUT_META_KEY, {**previous, "status": "stale"})
    return invalidated


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
            validate_asset_references(store, project_id, layers)
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
            existing_layer = next(
                (item for item in layers if item.layer_id == layer_id), None
            )
            if existing_layer is None:
                raise RasterLayerError(f"capa inexistente: {layer_id}")
            replacement = replace(existing_layer, asset_id=body.asset_id)
            validate_asset_references(store, project_id, [replacement])
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
            png, evidence = await run_in_threadpool(
                composite_layers, store, project_id, layers
            )
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

    @app.post(
        "/studio/api/projects/{project_id}/raster-layers/chroma-key", status_code=201
    )
    async def chroma_key_project_video(
        project_id: str, body: ChromaKeyRequest, request: Request
    ) -> dict[str, Any]:
        """Chroma-key CPU real sobre un vídeo del proyecto (C29/A17).

        Produce una secuencia portadora de alpha (ffv1 rgba/bgra lossless) y un
        frame RGBA de referencia, con provenance (sha256 de la fuente), probe de
        ffprobe y cobertura medida sobre frames decodificados. NO es matting
        semántico ni general: solo el color plano explícito elegido se vuelve
        transparente.
        """
        store = _store(request)
        _require_project(store, project_id)
        config = request.app.state.config
        try:
            video_bytes, frame_png, evidence = await run_in_threadpool(
                chroma_key_video,
                store,
                project_id,
                body.asset_id,
                body.key_color,
                similarity=body.similarity,
                blend=body.blend,
                ffmpeg_timeout_s=config.studio_ffmpeg_timeout_s,
                ffprobe_timeout_s=config.studio_ffprobe_timeout_s,
            )
        except ChromaKeyError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        output_sha256 = hashlib.sha256(video_bytes).hexdigest()
        chroma_meta = {
            "artifact_kind": "chroma_key_alpha_sequence",
            "renderer": evidence["renderer"],
            "matting": evidence["matting"],
            "semantic_matting": evidence["semantic_matting"],
            "key_color": evidence["key_color"],
            "similarity": evidence["similarity"],
            "blend": evidence["blend"],
            "source_asset_id": evidence["source"]["asset_id"],
            "source_sha256": evidence["source"]["sha256"],
            "output_sha256": output_sha256,
        }
        asset = store.save_upload(
            project_id,
            filename="chroma-key-alpha.mkv",
            content_type="video/x-matroska",
            data=video_bytes,
            max_upload_bytes=config.max_upload_bytes,
            category="outputs",
            metadata={"chroma_key": chroma_meta, "pixel_render": "ffmpeg-colorkey-cpu"},
        )
        frame_asset = store.save_upload(
            project_id,
            filename="chroma-key-frame-0000.png",
            content_type="image/png",
            data=frame_png,
            max_upload_bytes=config.max_upload_bytes,
            category="outputs",
            metadata={
                "chroma_key": chroma_meta,
                "artifact_kind": "chroma_key_frame",
                "pixel_render": "ffmpeg-colorkey-cpu",
            },
        )
        current_chroma_inputs = {
            "source_asset_id": body.asset_id,
            "source_sha256": evidence["source"]["sha256"],
            "key_color": evidence["key_color"],
            "similarity": evidence["similarity"],
            "blend": evidence["blend"],
        }
        invalidated_outputs = _mark_previous_chroma_output_stale(
            store, project_id, current_chroma_inputs
        )
        store.set_meta(
            project_id,
            _CHROMA_OUTPUT_META_KEY,
            {
                **current_chroma_inputs,
                "asset_id": asset["id"],
                "frame_asset_id": frame_asset["id"],
                "status": "current",
            },
        )
        return {
            "asset": asset,
            "frame_asset": frame_asset,
            "evidence": evidence,
            "invalidated_outputs": invalidated_outputs,
            "matting": evidence["matting"],
            "semantic_matting": evidence["semantic_matting"],
            "claim": "chroma-key de un color plano explícito; no matting semántico",
        }


__all__ = ["register_routes"]
