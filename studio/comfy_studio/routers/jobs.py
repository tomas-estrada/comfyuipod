"""Jobs: crear, consultar y reportar progreso. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request

from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    JobCreateRequest,
    JobProgressRequest,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.post("/studio/api/jobs", status_code=201)
    async def create_job(
        body: JobCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if body.project_id and active_store.get_project(body.project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        job = active_store.create_job(
            kind=body.kind,
            project_id=body.project_id,
            template_id=body.template_id,
            prompt_id=body.prompt_id,
            params=body.params,
        )
        active_broker.publish(
            "job",
            {
                "job_id": job["id"],
                "project_id": job["project_id"],
                "kind": job["kind"],
                "status": job["status"],
                "template_id": job["template_id"],
            },
        )
        return job
    @app.get("/studio/api/jobs")
    async def list_jobs(
        request: Request,
        project_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        return {
            "jobs": active_store.list_jobs(
                project_id=project_id,
                status=status,
            )
        }
    @app.get("/studio/api/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        job = active_store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job no encontrado")
        return job
    @app.post("/studio/api/jobs/{job_id}/progress")
    async def report_job_progress(
        job_id: str,
        body: JobProgressRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        updated = active_store.update_job(
            job_id,
            status=body.status,
            prompt_id=body.prompt_id,
            progress_value=body.progress_value,
            progress_max=body.progress_max,
            current_node=body.current_node,
            result=body.result,
            error=body.error,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Job no encontrado")
        active_broker.publish(
            "progress",
            {
                "job_id": updated["id"],
                "project_id": updated["project_id"],
                "status": updated["status"],
                "value": updated["progress_value"],
                "max": updated["progress_max"],
                "node": updated["current_node"],
            },
        )
        active_broker.publish(
            "job",
            {
                "job_id": updated["id"],
                "project_id": updated["project_id"],
                "kind": updated["kind"],
                "status": updated["status"],
                "error": updated["error"],
            },
        )
        return updated
