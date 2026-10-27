"""Aplicación FastAPI principal de Comfy Studio (127.0.0.1:18190, /studio/api/*)."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import (
    StudioConfig,
    install_log_redaction,
)
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.stubs.llm import StubLLMClient

logger = logging.getLogger("comfy_studio.app")


class ProjectCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    aspect_ratio: str = Field(default="16:9", max_length=32)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    aspect_ratio: str | None = Field(default=None, max_length=32)
    metadata: dict[str, Any] | None = None


class JobCreateRequest(BaseModel):
    kind: str = Field(default="render", max_length=64)
    project_id: str | None = None
    template_id: str | None = None
    prompt_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class JobProgressRequest(BaseModel):
    status: str | None = None
    prompt_id: str | None = None
    progress_value: int | None = None
    progress_max: int | None = None
    current_node: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


def _verify_basic_auth(
    auth_header: str | None,
    expected_user: str,
    expected_pass: str | None,
) -> bool:
    if not expected_pass or not auth_header:
        return False
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    if ":" not in decoded:
        return False
    user, password = decoded.split(":", 1)
    user_ok = secrets.compare_digest(user, expected_user)
    pass_ok = secrets.compare_digest(password, expected_pass)
    return user_ok and pass_ok


async def _probe_comfyui(
    comfyui_url: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    target = f"{comfyui_url.rstrip('/')}/system_stats"
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(2.0, connect=1.0),
        ) as client:
            resp = await client.get(target)
            if resp.status_code == 200:
                data = resp.json()
                return True, data if isinstance(data, dict) else {}
    except Exception:
        pass
    return False, None


def create_app(
    config: StudioConfig | None = None,
    *,
    comfyui_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = config if config is not None else StudioConfig.from_env()
    redactor = cfg.redactor()
    install_log_redaction(redactor)
    install_log_redaction(redactor, logger=logging.getLogger("comfy_studio"))

    store = ProjectStore(cfg.studio_data_dir)
    broker = ProgressBroker()
    stub_llm = StubLLMClient(model=cfg.studio_llm_model) if cfg.studio_stub_llm else None

    app = FastAPI(
        title="Comfy Studio Server",
        version="0.1.0",
    )
    app.state.config = cfg
    app.state.redactor = redactor
    app.state.store = store
    app.state.broker = broker
    app.state.stub_llm = stub_llm
    app.state.comfyui_transport = comfyui_transport
    app.state.comfyui_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=comfyui_transport,
        store=store,
        broker=broker,
        redactor=redactor,
    )

    @app.middleware("http")
    async def edge_auth_and_redaction_middleware(request: Request, call_next):
        active_cfg: StudioConfig = request.app.state.config
        env_pass = (
            os.environ.get("COMFY_PASS", "").strip()
            or os.environ.get("STUDIO_AUTH_PASS", "").strip()
            or None
        )
        env_user = (
            os.environ.get("COMFY_USER", "").strip()
            or os.environ.get("STUDIO_AUTH_USER", "").strip()
            or active_cfg.comfy_user
        )
        effective_pass = active_cfg.get_comfy_pass() or env_pass
        require_auth = (
            active_cfg.require_auth
            or bool(effective_pass)
            or os.environ.get("STUDIO_REQUIRE_AUTH", "").strip() in {"1", "true", "yes"}
            or request.headers.get("X-Studio-Simulate-Edge") == "1"
        )

        if require_auth and request.url.path.startswith(("/studio", "/health")):
            auth_header = request.headers.get("Authorization")
            if not _verify_basic_auth(auth_header, env_user, effective_pass):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Credenciales requeridas"},
                    headers={"WWW-Authenticate": 'Basic realm="ComfyUI Vast.ai Access"'},
                )

        return await call_next(request)

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
        payload = {
            "status": "ok",
            "server": "ok",
            "comfyui": "ok" if reachable else "degraded",
            "comfyui_reachable": reachable,
            "comfyui_url": active_cfg.studio_comfyui_url,
            "comfyui_devices": len((stats or {}).get("devices", [])) if stats else 0,
            "model_profile": active_cfg.model_profile,
            "stub_llm": active_cfg.studio_stub_llm,
            "llm_model": active_cfg.studio_llm_model,
            "llm_key_status": active_cfg.masked_llm_key_status(),
            "schema_version": active_store.get_schema_version(),
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
                "stub": active_cfg.studio_stub_llm,
            },
            "env_masked": active_cfg.to_public_dict(),
        }
        return active_redactor.redact_value(payload)

    # --- CRUD Proyectos ---

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

    # --- Subida y gestión de medios ---

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
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        path = active_store.get_asset_path(project_id, asset_id)
        if asset is None or path is None:
            raise HTTPException(status_code=404, detail="Archivo de medio no encontrado")
        return Response(
            content=path.read_bytes(),
            media_type=asset["mime_type"],
            headers={
                "Content-Disposition": f'inline; filename="{asset["original_filename"]}"'
            },
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

    # --- Jobs y canal SSE de progreso ---

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

    @app.get("/studio/api/progress")
    async def sse_progress(
        request: Request,
        heartbeat_interval: float | None = Query(default=None, ge=0.01, le=300.0),
        replay: bool = Query(default=False),
        max_events: int | None = Query(default=None, ge=1, le=1000),
    ) -> StreamingResponse:
        active_cfg: StudioConfig = request.app.state.config
        active_broker: ProgressBroker = request.app.state.broker
        interval = (
            heartbeat_interval
            if heartbeat_interval is not None
            else active_cfg.sse_heartbeat_seconds
        )
        return StreamingResponse(
            active_broker.stream(
                heartbeat_interval=interval,
                replay=replay,
                max_events=max_events,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app


app = create_app()
