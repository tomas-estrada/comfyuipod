"""CRUD de proyectos. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request

from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    ProjectCreateRequest,
    ProjectUpdateRequest,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.post("/studio/api/projects", status_code=201)
    async def create_project(
        body: ProjectCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        try:
            return active_store.create_project(
                name=body.name,
                description=body.description,
                aspect_ratio=body.aspect_ratio,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @app.patch("/studio/api/projects/{project_id}")
    @app.put("/studio/api/projects/{project_id}")
    async def update_project(
        project_id: str,
        body: ProjectUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        try:
            updated = active_store.update_project(
                project_id,
                name=body.name,
                description=body.description,
                aspect_ratio=body.aspect_ratio,
                metadata=body.metadata,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return updated
    @app.delete("/studio/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        deleted = active_store.delete_project(project_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return {"deleted": True, "id": project_id}
