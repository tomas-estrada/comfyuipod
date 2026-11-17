"""Persistencia de AOV e intervención de iluminación CPU por proyecto (W16/A18)."""

from __future__ import annotations

import base64
import binascii
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, StrictFloat, StrictInt
from starlette.concurrency import run_in_threadpool

from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.relight import (
    AOV,
    AOVSet,
    AOVValidity,
    RelightError,
    TaskKind,
    invalidate_for_task,
    relight_video_temporal,
    render_relight,
)

_META_KEY = "ultra_aov_set_v1"


class AOVRequest(BaseModel):
    validity: AOVValidity
    content_hash: str | None = None


class AOVSetRequest(BaseModel):
    aovs: dict[str, AOVRequest] = Field(default_factory=dict)


class InvalidationRequest(BaseModel):
    task: TaskKind


class RelightRenderRequest(BaseModel):
    """Buffers base64 estrictos + dimensiones; el composite es CPU determinista."""

    albedo: str
    shading: str
    width: int
    height: int
    intensity: float = 1.0
    ambient: float = 0.0


class TemporalLightKeyframe(BaseModel):
    frame: StrictInt
    gain: StrictFloat


class TemporalRelightRequest(BaseModel):
    keyframes: list[TemporalLightKeyframe] = Field(min_length=2, max_length=64)


def _decode_b64(value: str, name: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RelightError(f"{name} no es base64 válido: {exc}") from exc


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> AOVSet | None:
    raw = store.get_meta(project_id, _META_KEY)
    return AOVSet.from_dict(raw) if raw is not None else None


def _response(aovset: AOVSet) -> dict[str, Any]:
    return {**aovset.to_dict(), "render_validation": "not_run"}


def register_routes(app: FastAPI) -> None:
    """Guarda estado de pases y persiste invalidación antes de recomputación."""

    @app.post(
        "/studio/api/projects/{project_id}/assets/{asset_id}/relight-temporal",
        status_code=201,
    )
    async def relight_project_video_temporal(
        project_id: str, asset_id: str, body: TemporalRelightRequest, request: Request
    ) -> dict[str, Any]:
        """Apply a bounded CPU light-gain curve to a video asset owned by this project."""
        store = _store(request)
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        source = store.get_asset(project_id, asset_id)
        source_path = store.get_asset_path(project_id, asset_id)
        if source is None or source.get("kind") != "video" or source_path is None:
            raise HTTPException(status_code=404, detail="Video del proyecto no encontrado")
        tmpdir = Path(tempfile.mkdtemp(prefix="studio-temporal-relight-"))
        rendered = tmpdir / "relighted.mp4"
        try:
            report = await run_in_threadpool(
                relight_video_temporal,
                source_path,
                rendered,
                [point.model_dump() for point in body.keyframes],
            )
            max_bytes = min(request.app.state.config.max_upload_bytes, 256 * 1024 * 1024)
            output = store.save_upload(
                project_id,
                filename=f"{Path(source['original_filename']).stem}-relighted.mp4",
                content_type="video/mp4",
                data=rendered.read_bytes(),
                max_upload_bytes=max_bytes,
                category="outputs",
                metadata={
                    "temporal_relight_v1": {
                        "source_asset_id": asset_id,
                        **report,
                    }
                },
            )
            return {
                "status": "rendered",
                "project_id": project_id,
                "source_asset_id": asset_id,
                "asset": output,
                "relighting": report,
            }
        except (RelightError, MediaValidationError, OSError, ValueError) as exc:
            status = exc.status_code if isinstance(exc, MediaValidationError) else 422
            detail = exc.message if isinstance(exc, MediaValidationError) else str(exc)
            raise HTTPException(status_code=status, detail=detail) from exc
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @app.get("/studio/api/projects/{project_id}/aovs")
    async def get_project_aovs(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = _load(store, project_id)
        except RelightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if aovset is None:
            raise HTTPException(status_code=404, detail="AOVSet no encontrado")
        return _response(aovset)

    @app.put("/studio/api/projects/{project_id}/aovs")
    async def put_project_aovs(
        project_id: str,
        body: AOVSetRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = AOVSet(
                aovs={
                    name: AOV(
                        name=name,
                        validity=item.validity,
                        content_hash=item.content_hash,
                    )
                    for name, item in body.aovs.items()
                }
            )
            aovset.validate()
            store.set_meta(project_id, _META_KEY, aovset.to_dict())
            return _response(aovset)
        except RelightError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/aovs/invalidate")
    async def invalidate_project_aovs(
        project_id: str,
        body: InvalidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = _load(store, project_id)
        except RelightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if aovset is None:
            raise HTTPException(status_code=404, detail="AOVSet no encontrado")
        invalidated = sorted(invalidate_for_task(aovset, body.task))
        for name in invalidated:
            aovset.aovs[name].validity = AOVValidity.UNKNOWN
            aovset.aovs[name].content_hash = None
        store.set_meta(project_id, _META_KEY, aovset.to_dict())
        return {**_response(aovset), "task": body.task, "invalidated": invalidated}

    @app.post("/studio/api/projects/{project_id}/aovs/relight/render")
    async def render_project_relight(
        project_id: str,
        body: RelightRenderRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Reiluminación CPU determinista: albedo × shading → PNG en outputs.

        Decodifica base64 estricto (validate=True), compone con `render_relight`
        (pure CPU, sin GPU) y persiste el PNG como asset de categoría `outputs`.
        La evidencia (sha256 del PNG, del albedo y del shading) acompaña la
        respuesta sin el buffer de píxeles.
        """
        store = _store(request)
        _require_project(store, project_id)  # 404 antes de tocar píxeles
        try:
            albedo = _decode_b64(body.albedo, "albedo")
            shading = _decode_b64(body.shading, "shading")
            rendered = await run_in_threadpool(
                render_relight,
                albedo,
                shading,
                body.width,
                body.height,
                body.intensity,
                body.ambient,
            )
        except RelightError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        config = request.app.state.config
        try:
            asset = store.save_upload(
                project_id,
                filename="relight.png",
                content_type="image/png",
                data=rendered["png"],
                max_upload_bytes=config.max_upload_bytes,
                category="outputs",
                metadata={
                    "artifact_kind": "aov_relight",
                    "renderer": rendered["renderer"],
                    "width": rendered["width"],
                    "height": rendered["height"],
                    "color_type": rendered["color_type"],
                    "intensity": rendered["intensity"],
                    "ambient": rendered["ambient"],
                    "sha256": rendered["sha256"],
                    "albedo_sha256": rendered["albedo_sha256"],
                    "shading_sha256": rendered["shading_sha256"],
                },
            )
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        evidence = {key: value for key, value in rendered.items() if key != "png"}
        return {"asset": asset, "evidence": evidence}


__all__ = ["register_routes"]
