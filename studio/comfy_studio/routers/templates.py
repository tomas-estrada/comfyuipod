"""Plantillas: catálogo y submit (síncrono ?wait=1 / asíncrono 202).

Extraído de app.py (M7-SCR-002).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.config import (
    StudioConfig,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.templates import (
    TemplateNotFoundError,
    TemplateParamError,
    TemplateRegistry,
)
from comfy_studio.webutil import (
    _run_render_job_task,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/templates")
    async def list_templates_endpoint(
        request: Request,
        include_workflow: bool = Query(default=False),
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_cfg: StudioConfig = request.app.state.config
        # El registro HTTP expone SIEMPRE todas las plantillas; cada entrada declara
        # `available`/`unavailable_reason` (gating por pesos 3D, VAL-UNREAL-004)
        # y es el panel quien no ofrece las no disponibles.
        return {
            "templates": active_registry.list_templates(
                include_workflow=include_workflow,
                three_d_available=active_cfg.three_d_weights_available(),
                animate_available=active_cfg.animate_weights_available(),
                minimax_available=active_cfg.minimax_weights_available(),
            )
        }
    @app.get("/studio/api/templates/{template_id}")
    async def get_template_endpoint(
        template_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_cfg: StudioConfig = request.app.state.config
        try:
            return active_registry.get_template(
                template_id,
                include_workflow=True,
                three_d_available=active_cfg.three_d_weights_available(),
                animate_available=active_cfg.animate_weights_available(),
                minimax_available=active_cfg.minimax_weights_available(),
            )
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    @app.post("/studio/api/templates/{template_id}/submit")
    @app.post("/studio/api/projects/{project_id}/templates/{template_id}/submit")
    async def submit_template_endpoint(
        template_id: str,
        request: Request,
        project_id: str | None = None,
        wait: bool = Query(default=False),
    ):
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        active_redactor = active_cfg.redactor()

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Cuerpo JSON inválido: {exc}",
            ) from exc

        if not isinstance(payload, dict):
            raise HTTPException(
                status_code=422,
                detail="El cuerpo de la petición debe ser un objeto JSON.",
            )

        eff_project_id = project_id or payload.get("project_id")
        if "params" in payload:
            raw_params = payload.get("params")
        else:
            raw_params = {k: v for k, v in payload.items() if k != "project_id"}

        try:
            enqueued = await active_registry.enqueue_render(
                template_id,
                raw_params,
                project_id=eff_project_id,
                store=active_store,
                comfyui_client=active_comfyui,
                broker=active_broker,
                strict=True,
                three_d_available=active_cfg.three_d_weights_available(),
                animate_available=active_cfg.animate_weights_available(),
                minimax_available=active_cfg.minimax_weights_available(),
            )
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except TemplateParamError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content=active_redactor.redact_value(
                    {
                        "detail": exc.message,
                        "errors": exc.errors,
                    }
                ),
            )
        except ComfyUIError as exc:
            raise HTTPException(
                status_code=exc.status_code or 502,
                detail=active_redactor.redact(exc.message),
            ) from exc

        # M7-SCR-001: por defecto el submit es ASÍNCRONO (202 Accepted): el render corre
        # en segundo plano y el resultado se sigue con GET /studio/api/jobs/{job_id} y
        # por SSE /studio/api/progress (evita que un render largo agoste el timeout del
        # proxy de borde). `?wait=1` recupera el comportamiento síncrono (200 + result).
        if wait:
            try:
                result = await active_registry.execute_render_job(
                    enqueued["job_id"],
                    store=active_store,
                    comfyui_client=active_comfyui,
                    broker=active_broker,
                )
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ComfyUIError as exc:
                raise HTTPException(
                    status_code=exc.status_code or 502,
                    detail=active_redactor.redact(exc.message),
                ) from exc
            return active_redactor.redact_value(result)

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
                    "template_id": enqueued["template_id"],
                    "location": f"/studio/api/jobs/{job_id}",
                    "progress_url": "/studio/api/progress",
                }
            ),
        )
