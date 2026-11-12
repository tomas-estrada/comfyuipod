"""Manifiestos de escena 3D ligados a GLB poseídos por el proyecto."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio.asset3d_manifest import get_asset3d_manifest, save_asset3d_manifest
from comfy_studio.assets3d import Asset3DError
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import Asset3DManifestRequest


def register_routes(app: FastAPI) -> None:
    """Registra el acceso reabrible al manifiesto de un modelo GLB del proyecto."""

    @app.put("/studio/api/projects/{project_id}/assets/{asset_id}/3d-manifest")
    async def put_asset3d_manifest(
        project_id: str,
        asset_id: str,
        body: Asset3DManifestRequest,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            record = await run_in_threadpool(
                save_asset3d_manifest, store, project_id, asset_id, body.manifest
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Asset3DError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return redactor.redact_value(record)

    @app.get("/studio/api/projects/{project_id}/assets/{asset_id}/3d-manifest")
    async def get_asset3d_manifest_endpoint(
        project_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        redactor = request.app.state.config.redactor()
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            record = await run_in_threadpool(get_asset3d_manifest, store, project_id, asset_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Asset3DError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if record is None:
            raise HTTPException(status_code=404, detail="Manifiesto 3D no encontrado")
        return redactor.redact_value(record)
