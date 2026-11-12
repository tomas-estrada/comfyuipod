"""Assets/media/uploads, galería y /view. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response

from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.config import (
    StudioConfig,
)
from comfy_studio.inspiration import analyze_reference_asset, derive_recipe
from comfy_studio.projects import MediaValidationError, ProjectStore, sanitize_filename


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/projects")
    async def list_projects(request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        return {"projects": active_store.list_projects()}
    @app.get("/studio/api/projects/{project_id}")
    async def get_project(project_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        proj = active_store.get_project(project_id)
        if proj is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return proj
    @app.post("/studio/api/projects/{project_id}/assets", status_code=201)
    @app.post("/studio/api/projects/{project_id}/media", status_code=201)
    @app.post("/studio/api/projects/{project_id}/uploads", status_code=201)
    async def upload_project_media(
        project_id: str,
        request: Request,
        file: UploadFile = File(...),
        category: str = Form("uploads"),
    ) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store

        # Lectura acotada a max_upload_bytes + 1 para rechazar sin agotar memoria
        limit = active_cfg.max_upload_bytes
        data = await file.read(limit + 1)
        try:
            return active_store.save_upload(
                project_id,
                filename=file.filename,
                content_type=file.content_type,
                data=data,
                max_upload_bytes=limit,
                category=category,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except MediaValidationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    @app.get("/studio/api/projects/{project_id}/assets")
    @app.get("/studio/api/projects/{project_id}/media")
    async def list_project_assets(
        project_id: str,
        request: Request,
        kind: str | None = Query(default=None),
        category: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return {
            "assets": active_store.list_assets(
                project_id,
                kind=kind,
                category=category,
            )
        }
    @app.get("/studio/api/projects/{project_id}/assets/{asset_id}")
    @app.get("/studio/api/projects/{project_id}/media/{asset_id}")
    async def get_project_asset(
        project_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        if asset is None:
            raise HTTPException(status_code=404, detail="Medio no encontrado")
        return asset
    @app.get("/studio/api/projects/{project_id}/assets/{asset_id}/file")
    @app.get("/studio/api/projects/{project_id}/media/{asset_id}/file")
    async def download_project_asset_file(
        project_id: str,
        asset_id: str,
        request: Request,
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        path = active_store.get_asset_path(project_id, asset_id)
        if asset is None or path is None:
            raise HTTPException(status_code=404, detail="Archivo de medio no encontrado")
        disposition = "attachment" if download else "inline"
        return Response(
            content=path.read_bytes(),
            media_type=asset["mime_type"],
            headers={
                "Content-Disposition": f'{disposition}; filename="{asset["original_filename"]}"'
            },
        )
    @app.get("/studio/api/projects/{project_id}/gallery")
    async def list_project_gallery(
        project_id: str,
        request: Request,
        kind: str | None = Query(default=None),
        category: str = Query(default="outputs"),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        eff_category = None if category == "all" else category
        items = active_store.list_assets(
            project_id,
            kind=kind,
            category=eff_category,
        )
        return {
            "project_id": project_id,
            "assets": items,
            "outputs": items,
        }
    @app.get("/studio/api/view")
    async def view_media_output(
        request: Request,
        project_id: str | None = Query(default=None),
        asset_id: str | None = Query(default=None),
        filename: str | None = Query(default=None),
        subfolder: str = Query(default=""),
        type: str = Query(default="output"),
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        disposition = "attachment" if download else "inline"

        if project_id and asset_id:
            asset = active_store.get_asset(project_id, asset_id)
            path = active_store.get_asset_path(project_id, asset_id)
            if asset is None or path is None:
                raise HTTPException(status_code=404, detail="Asset de galería no encontrado")
            return Response(
                content=path.read_bytes(),
                media_type=asset["mime_type"],
                headers={
                    "Content-Disposition": (
                        f'{disposition}; filename="{asset["original_filename"]}"'
                    )
                },
            )

        if project_id and filename:
            for candidate in active_store.list_assets(project_id):
                if filename in {
                    candidate.get("filename"),
                    candidate.get("original_filename"),
                    (candidate.get("metadata") or {}).get("comfy_filename"),
                }:
                    cpath = active_store.get_asset_path(project_id, candidate["id"])
                    if cpath is not None:
                        return Response(
                            content=cpath.read_bytes(),
                            media_type=candidate["mime_type"],
                            headers={
                                "Content-Disposition": (
                                    f'{disposition}; filename="{candidate["original_filename"]}"'
                                )
                            },
                        )

        if filename:
            # Do not delegate path validation to ComfyUI's /view endpoint.
            # Reject separators and traversal components before proxying.
            for label, value in (("filename", filename), ("subfolder", subfolder or "")):
                if "/" in value or "\\" in value or ".." in value.split("/"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"{label} contiene componentes de ruta no permitidos",
                    )
            try:
                raw_bytes = await active_comfyui.view_output(
                    filename,
                    subfolder=subfolder,
                    storage_type=type,
                )
            except ComfyUIError as exc:
                raise HTTPException(
                    status_code=exc.status_code or 404,
                    detail=exc.message,
                ) from exc
            media_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/png"
            # El nombre viaja a Content-Disposition: sanear (sin comillas ni CR/LF).
            safe_name = sanitize_filename(os.path.basename(filename), fallback_ext=".bin")
            return Response(
                content=raw_bytes,
                media_type=media_type,
                headers={"Content-Disposition": f'{disposition}; filename="{safe_name}"'},
            )

        raise HTTPException(
            status_code=400,
            detail="Debe especificar 'project_id' + 'asset_id' o 'filename'.",
        )
    @app.delete("/studio/api/projects/{project_id}/assets/{asset_id}")
    @app.delete("/studio/api/projects/{project_id}/media/{asset_id}")
    async def delete_project_asset(
        project_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        deleted = active_store.delete_asset(project_id, asset_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Medio no encontrado")
        return {"deleted": True, "id": asset_id}

    @app.post("/studio/api/projects/{project_id}/assets/{asset_id}/inspiration/analyze")
    def analyze_project_reference(
        project_id: str,
        asset_id: str,
        request: Request,
        brief_shots: int | None = Query(default=None, ge=1, le=100),
        scene_threshold: float = Query(default=0.25, ge=0.05, le=0.95),
    ) -> dict[str, Any]:
        """Analyze an authorized project video locally and return its editable recipe."""
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            analysis = analyze_reference_asset(
                active_store,
                project_id,
                asset_id,
                scene_threshold=scene_threshold,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        recipe = derive_recipe(analysis, brief_shots=brief_shots)
        return {
            "analysis": analysis.to_dict(),
            "recipe": recipe.to_dict(),
            "simulated": False,
            "network_used": False,
        }
