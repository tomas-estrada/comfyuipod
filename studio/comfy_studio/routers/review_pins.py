"""Review pins: feedback anclado a timecode sobre versiones de EDL (M3-RVP-001)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.review_pins import open_pin_summary
from comfy_studio.webutil import (
    ReviewPinCreateRequest,
    ReviewPinUpdateRequest,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.post("/studio/api/projects/{project_id}/pins", status_code=201)
    async def create_review_pin_endpoint(
        project_id: str,
        body: ReviewPinCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Crea un review pin anclado a una versión de EDL (M3-RVP-001)."""
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        edl = active_store.get_edl_version(project_id, body.edl_version)
        if edl is None:
            raise HTTPException(
                status_code=404,
                detail=f"Versión de EDL no encontrada: {body.edl_version}",
            )
        try:
            pin = active_store.create_review_pin(
                project_id,
                edl_version=body.edl_version,
                t_start=body.t_start,
                t_end=body.t_end,
                severity=body.severity,
                body=body.body,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        active_broker.publish(
            "review_pin",
            {
                "project_id": project_id,
                "pin_id": pin["id"],
                "edl_version": pin["edl_version"],
                "t_start": pin["t_start"],
                "t_end": pin["t_end"],
                "severity": pin["severity"],
                "status": pin["status"],
                "action": "added",
            },
        )
        return pin

    @app.get("/studio/api/projects/{project_id}/pins")
    async def list_review_pins_endpoint(
        project_id: str,
        request: Request,
        edl_version: int | None = Query(default=None, ge=1),
        status: str | None = Query(default=None, pattern=r"^(open|resolved|discarded)$"),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            pins = active_store.list_review_pins(
                project_id,
                edl_version=edl_version,
                status=status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"pins": pins, "count": len(pins)}

    @app.get("/studio/api/projects/{project_id}/pins/summary")
    async def review_pins_summary_endpoint(
        project_id: str,
        request: Request,
        edl_version: int | None = Query(default=None, ge=1),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        pins = active_store.list_review_pins(
            project_id,
            edl_version=edl_version,
            status=None,
        )
        return {"summary": open_pin_summary(pins), "total": len(pins)}

    @app.patch("/studio/api/projects/{project_id}/pins/{pin_id}")
    async def update_review_pin_endpoint(
        project_id: str,
        pin_id: str,
        body: ReviewPinUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        current = active_store.get_review_pin(project_id, pin_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Pin no encontrado")
        try:
            updated = active_store.update_review_pin(
                project_id,
                pin_id,
                t_start=body.t_start,
                t_end=body.t_end,
                status=body.status,
                resolution_note=body.resolution_note,
                resolved_by="reviewer",
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if updated is None:  # pragma: no cover - cubierto por el 404 previo
            raise HTTPException(status_code=404, detail="Pin no encontrado")
        if body.status is not None and body.status != "open":
            active_broker.publish(
                "review_pin",
                {
                    "project_id": project_id,
                    "pin_id": updated["id"],
                    "edl_version": updated["edl_version"],
                    "t_start": updated["t_start"],
                    "t_end": updated["t_end"],
                    "severity": updated["severity"],
                    "status": updated["status"],
                    "action": "resolved",
                },
            )
        return updated

    @app.delete("/studio/api/projects/{project_id}/pins/{pin_id}")
    async def delete_review_pin_endpoint(
        project_id: str,
        pin_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            deleted = active_store.delete_review_pin(project_id, pin_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Pin no encontrado")
        return {"deleted": True, "pin_id": pin_id}
