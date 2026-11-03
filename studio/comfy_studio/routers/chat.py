"""Chat del agente LLM por proyecto (SSE opcional). (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from comfy_studio.agent import StudioAgent
from comfy_studio.progress import format_sse_event
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    ChatTurnRequest,
)

logger = logging.getLogger(__name__)

def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.post("/studio/api/projects/{project_id}/chat")
    async def project_chat_turn(
        project_id: str,
        body: ChatTurnRequest,
        request: Request,
    ):
        active_store: ProjectStore = request.app.state.store
        active_agent: StudioAgent = request.app.state.agent
        active_redactor = request.app.state.config.redactor()
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        wants_stream = body.stream or "text/event-stream" in (request.headers.get("accept") or "")
        if wants_stream:

            async def _sse_gen():
                try:
                    async for ev in active_agent.stream_turn(
                        project_id=project_id,
                        user_message=body.message,
                        asset_ids=body.asset_ids,
                        keyframe_data_urls=body.keyframe_data_urls,
                        max_iterations=body.max_iterations,
                    ):
                        yield format_sse_event(ev["type"], active_redactor.redact_value(ev))
                except Exception as exc:
                    logger.warning(
                        "Error no controlado en el stream SSE del turno de chat: %s",
                        active_redactor.redact(str(exc)),
                    )
                    err_ev = active_redactor.redact_value({"type": "error", "detail": str(exc)})
                    yield format_sse_event("error", err_ev)

            return StreamingResponse(
                _sse_gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        try:
            turn_result = await active_agent.run_turn(
                project_id=project_id,
                user_message=body.message,
                asset_ids=body.asset_ids,
                keyframe_data_urls=body.keyframe_data_urls,
                max_iterations=body.max_iterations,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=active_redactor.redact(str(exc)),
            ) from exc
        return active_redactor.redact_value(turn_result.to_dict())
    @app.get("/studio/api/projects/{project_id}/chat")
    async def get_project_chat_history(
        project_id: str,
        request: Request,
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_redactor = request.app.state.config.redactor()
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        payload = {
            "project_id": project_id,
            "messages": active_store.list_chat_messages(project_id, limit=limit),
        }
        return active_redactor.redact_value(payload)
    @app.delete("/studio/api/projects/{project_id}/chat")
    async def clear_project_chat_history(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        removed = active_store.clear_chat_messages(project_id)
        return {"deleted": True, "project_id": project_id, "removed_count": removed}
