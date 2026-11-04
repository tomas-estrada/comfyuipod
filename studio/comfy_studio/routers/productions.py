"""Orquestación de producciones (plan/approve/status). (extraído de app.py, M7-SCR-002)"""

# GOBERNANZA (M6-AGT-001): la puerta de gasto humano. `plan` deja la
# producción en `awaiting_approval` SIN gastar GPU; el lazo caro (ejecución
# + ensamblaje) solo corre tras `approve`, y `approve` SOLO vive aquí, en la
# API pública accionada por el usuario. El agente LLM NUNCA ve `approve`
# (ver BELT_TOOLS_EXPOSED en mcp_server.py). `approve` despacha el lazo como
# tarea de fondo (run_in_threadpool) para no bloquear Nginx (504).





# --- Subida y gestión de medios ---







# --- Registro de plantillas (list / get / submit) ---





# --- Jobs y canal SSE de progreso ---


from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio.orchestration.service import (
    ProductionAlreadyTerminalError,
    ProductionNotApprovedError,
    ProductionNotFoundError,
    ProductionPlanNotEditableError,
    ProductionService,
)
from comfy_studio.webutil import (
    ProductionPlanRequest,
    ProductionPlanUpdateRequest,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/productions")
    async def list_productions_route(
        request: Request,
        project_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        return {
            "productions": await run_in_threadpool(
                service.list_productions,
                project_id=project_id,
                limit=limit,
            )
        }
    @app.post("/studio/api/productions", status_code=201)
    async def plan_production_route(
        body: ProductionPlanRequest,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(
                service.plan_production,
                body.idea,
                cost_cap_usd=body.cost_cap_usd,
                project_id=body.project_id,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @app.get("/studio/api/productions/{production_id}")
    async def get_production_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(service.get_plan, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    @app.get("/studio/api/productions/{production_id}/status")
    async def production_status_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(service.production_status, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    @app.post("/studio/api/productions/{production_id}/approve")
    async def approve_production_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        """Aprobación HUMANA de la puerta de gasto: corre el lazo caro de fondo."""
        service: ProductionService = request.app.state.production_service
        try:
            # corre el grafo caro fuera del event loop para no bloquear Nginx.
            return await run_in_threadpool(service.approve_plan, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionNotApprovedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ProductionAlreadyTerminalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    @app.patch("/studio/api/productions/{production_id}/plan")
    async def update_plan_route(
        production_id: str,
        body: ProductionPlanUpdateRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Edita el plan visible SOLO mientras espera aprobación humana (409 si no)."""
        service: ProductionService = request.app.state.production_service
        redactor = request.app.state.config.redactor()
        try:
            return await run_in_threadpool(
                service.update_plan,
                production_id,
                idea=body.idea,
                shots=(
                    [s.model_dump() for s in body.shots]
                    if body.shots is not None
                    else None
                ),
            )
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionPlanNotEditableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, LookupError) as exc:
            raise HTTPException(
                status_code=400, detail=redactor.redact(str(exc))
            ) from exc
    @app.post("/studio/api/productions/{production_id}/cancel")
    async def cancel_production_route(
        production_id: str,
        request: Request,
    ) -> dict[str, Any]:
        """Cancela la producción (terminal si espera aprobación; cooperativo si ejecuta)."""
        service: ProductionService = request.app.state.production_service
        try:
            return await run_in_threadpool(service.cancel_production, production_id)
        except ProductionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ProductionAlreadyTerminalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
