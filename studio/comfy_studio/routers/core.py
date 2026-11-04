"""Rutas core de Studio: health, status y catálogo de tools. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from comfy_studio.agent import StudioAgent
from comfy_studio.agent_brain.registry import brain_health_report, build_default_brain
from comfy_studio.config import (
    StudioConfig,
)
from comfy_studio.engines.health import health_report
from comfy_studio.engines.registry import build_default_registry
from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.orchestration.health import orchestration_health_report
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    _edge_auth_enforced,
    _probe_comfyui,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/health")
    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store
        active_redactor = active_cfg.redactor()
        reachable, stats = await _probe_comfyui(
            active_cfg.studio_comfyui_url,
            transport=request.app.state.comfyui_transport,
        )
        perception_available = bool(getattr(request.app.state, "perception_available", False))
        perception_error = getattr(request.app.state, "perception_error", None)
        payload = {
            "status": "ok",
            "server": "ok",
            "comfyui": "ok" if reachable else "degraded",
            "comfyui_reachable": reachable,
            "comfyui_url": active_cfg.studio_comfyui_url,
            "comfyui_devices": len((stats or {}).get("devices", [])) if stats else 0,
            "model_profile": active_cfg.model_profile,
            "edge_auth": "basic" if _edge_auth_enforced(active_cfg) else "open",
            "stub_llm": active_cfg.studio_stub_llm,
            "llm_model": active_cfg.studio_llm_model,
            "llm_key_status": active_cfg.masked_llm_key_status(),
            "llm_fallback_key_status": active_cfg.masked_llm_fallback_key_status(),
            # M1.2/M3: la percepción es un subsistema degradable e independiente del
            # resto de Studio; su estado debe ser observable sin abrir un log.
            "perception": {
                "available": perception_available,
                "backend": getattr(request.app.state, "perception_backend", "sim"),
                "reason": perception_error,
                "simulated": getattr(request.app.state, "perception_backend", "sim") == "sim",
                "stale": bool(getattr(request.app.state, "perception_stale", False)),
                "probe": getattr(request.app.state, "perception_probe_result", None),
            },
            # Runtime media prerequisites are resolved through PATH so operators can
            # diagnose degraded EDL/keyframe features consistently on any host OS.
            "media_tools": {
                "ffmpeg_available": find_ffmpeg() is not None,
                "ffprobe_available": find_ffprobe() is not None,
                "sqlite_vec_available": perception_available,
                "perception_backend": getattr(request.app.state, "perception_backend", "sim"),
                "simulated": getattr(request.app.state, "perception_backend", "sim") == "sim",
            },
            "schema_version": active_store.get_schema_version(),
            # M5-F7: estado por motor del roster (ready/stub/unwired) para que el
            # strict-check del supervisor observe cada motor, no solo que el studio vive.
            "engines": health_report(build_default_registry()),
            # M6-B1: estado del cerebro del agente (memory/compress/retrieve/rerank).
            "agent_brain": brain_health_report(build_default_brain()),
            # M6-B5: estado de la capa de orquestación (grafo/servicio/LangGraph).
            "orchestration": orchestration_health_report(),
        }
        return active_redactor.redact_value(payload)
    @app.get("/studio/api/status")
    async def studio_status(request: Request) -> dict[str, Any]:
        active_cfg: StudioConfig = request.app.state.config
        active_store: ProjectStore = request.app.state.store
        active_redactor = active_cfg.redactor()
        reachable, stats = await _probe_comfyui(
            active_cfg.studio_comfyui_url,
            transport=request.app.state.comfyui_transport,
        )
        projects_list = active_store.list_projects()
        jobs_list = active_store.list_jobs()
        payload = {
            "status": "ok",
            "comfyui": {
                "status": "ok" if reachable else "degraded",
                "reachable": reachable,
                "url": active_cfg.studio_comfyui_url,
                "system_stats": stats,
            },
            "db": {
                "schema_version": active_store.get_schema_version(),
                "projects_count": len(projects_list),
                "jobs_count": len(jobs_list),
            },
            "llm": {
                "base_url": active_cfg.studio_llm_base_url,
                "model": active_cfg.studio_llm_model,
                "fallback_base_url": active_cfg.studio_llm_fallback_base_url,
                "fallback_model": active_cfg.studio_llm_fallback_model,
                "key_status": active_cfg.masked_llm_key_status(),
                "fallback_key_status": active_cfg.masked_llm_fallback_key_status(),
                "stub": active_cfg.studio_stub_llm,
            },
            "env_masked": active_cfg.to_public_dict(),
        }
        return active_redactor.redact_value(payload)
    @app.get("/studio/api/agent/tools")
    @app.get("/studio/api/tools")
    async def list_agent_tools(request: Request) -> dict[str, Any]:
        active_agent: StudioAgent = request.app.state.agent
        return {"tools": active_agent.tool_belt.to_openai_tools()}
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)
