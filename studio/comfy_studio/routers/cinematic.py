"""Cinematic image composition through the existing local template/job pipeline."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.config import StudioConfig
from comfy_studio.creative.cinematic import (
    CinematicRequestError,
    cinematic_options,
    prepare_cinematic_render,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.templates import TemplateParamError, TemplateRegistry
from comfy_studio.webutil import _run_render_job_task

CINEMATIC_TEMPLATE_ID = "sdxl_image"


def register_routes(app: FastAPI) -> None:
    """Register the local cinematic direction endpoints."""

    @app.get("/studio/api/cinematic/options")
    async def get_cinematic_options() -> dict[str, Any]:
        return cinematic_options()

    @app.post("/studio/api/projects/{project_id}/cinematic/submit")
    async def submit_cinematic_render(project_id: str, request: Request):
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        active_redactor = active_cfg.redactor()

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail="Cuerpo JSON inválido.") from exc

        try:
            prepared = prepare_cinematic_render(payload)
        except CinematicRequestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail=f"Proyecto {project_id!r} no encontrado.")

        try:
            enqueued = await active_registry.enqueue_render(
                CINEMATIC_TEMPLATE_ID,
                prepared["params"],
                project_id=project_id,
                store=active_store,
                comfyui_client=active_comfyui,
                broker=active_broker,
                strict=True,
                three_d_available=active_cfg.three_d_weights_available(),
                animate_available=active_cfg.animate_weights_available(),
                minimax_available=active_cfg.minimax_weights_available(),
                qwen_layered_available=active_cfg.qwen_layered_weights_available(),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TemplateParamError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=active_redactor.redact_value(
                    {"detail": exc.message, "errors": exc.errors}
                ),
            )
        except ComfyUIError as exc:
            raise HTTPException(
                status_code=exc.status_code or 502,
                detail=active_redactor.redact(exc.message),
            ) from exc

        job_id = enqueued["job_id"]
        render_task = asyncio.create_task(_run_render_job_task(request.app, job_id))
        request.app.state.render_tasks.add(render_task)
        render_task.add_done_callback(request.app.state.render_tasks.discard)

        return JSONResponse(
            status_code=202,
            content=active_redactor.redact_value(
                {
                    "status": "queued",
                    "async": True,
                    "job_id": job_id,
                    "project_id": enqueued["project_id"],
                    "template_id": CINEMATIC_TEMPLATE_ID,
                    "location": f"/studio/api/jobs/{job_id}",
                    "progress_url": "/studio/api/progress",
                    "prompt_preview": prepared["params"]["prompt"],
                    "aspect_ratio": prepared["aspect_ratio"],
                    "width": prepared["params"]["width"],
                    "height": prepared["params"]["height"],
                    "seed": prepared["params"]["seed"],
                }
            ),
        )
