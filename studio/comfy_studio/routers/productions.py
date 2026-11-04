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

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio.orchestration.service import (
    ProductionNotApprovedError,
    ProductionNotFoundError,
    ProductionService,
)
from comfy_studio.webutil import (
    ProductionPlanRequest,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
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
