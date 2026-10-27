"""Aplicación FastAPI principal de Comfy Studio (127.0.0.1:18190, /studio/api/*)."""

from __future__ import annotations

import base64
import binascii
import logging
import os
import secrets
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from comfy_studio.agent import OpenAICompatibleLLMClient, StudioAgent, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient, ComfyUIError
from comfy_studio.config import (
    StudioConfig,
    install_log_redaction,
)
from comfy_studio.progress import ProgressBroker, format_sse_event
from comfy_studio.projects import MediaValidationError, ProjectStore
from comfy_studio.stubs.llm import StubLLMClient
from comfy_studio.templates import (
    TemplateNotFoundError,
    TemplateParamError,
    TemplateRegistry,
    get_default_registry,
)

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


class ChatTurnRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20000)
    asset_ids: list[str] = Field(default_factory=list)
    keyframe_data_urls: list[str] = Field(default_factory=list)
    stream: bool = Field(default=False)
    max_iterations: int | None = Field(default=None, ge=1, le=25)


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
    app.state.template_registry = get_default_registry()
    app.state.tool_belt = create_default_tool_belt()
    app.state.llm_client = OpenAICompatibleLLMClient(
        config=cfg,
        stub_client=stub_llm,
        redactor=redactor,
    )
    app.state.agent = StudioAgent(
        config=cfg,
        store=store,
        comfyui_client=app.state.comfyui_client,
        broker=broker,
        llm_client=app.state.llm_client,
        tool_belt=app.state.tool_belt,
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
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        asset = active_store.get_asset(project_id, asset_id)
        path = active_store.get_asset_path(project_id, asset_id)
        if asset is None or path is None:
            raise HTTPException(status_code=404, detail="Archivo de medio no encontrado")
        disposition = "attachment" if download else "inline"
        return Response(
            content=path.read_bytes(),
            media_type=asset["mime_type"],
            headers={
                "Content-Disposition": f'{disposition}; filename="{asset["original_filename"]}"'
            },
        )

    @app.get("/studio/api/projects/{project_id}/gallery")
    async def list_project_gallery(
        project_id: str,
        request: Request,
        kind: str | None = Query(default=None),
        category: str = Query(default="outputs"),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        eff_category = None if category == "all" else category
        items = active_store.list_assets(
            project_id,
            kind=kind,
            category=eff_category,
        )
        return {
            "project_id": project_id,
            "assets": items,
            "outputs": items,
        }

    @app.get("/studio/api/view")
    async def view_media_output(
        request: Request,
        project_id: str | None = Query(default=None),
        asset_id: str | None = Query(default=None),
        filename: str | None = Query(default=None),
        subfolder: str = Query(default=""),
        type: str = Query(default="output"),
        download: bool = Query(default=False),
    ) -> Response:
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        disposition = "attachment" if download else "inline"

        if project_id and asset_id:
            asset = active_store.get_asset(project_id, asset_id)
            path = active_store.get_asset_path(project_id, asset_id)
            if asset is None or path is None:
                raise HTTPException(status_code=404, detail="Asset de galería no encontrado")
            return Response(
                content=path.read_bytes(),
                media_type=asset["mime_type"],
                headers={
                    "Content-Disposition": (
                        f'{disposition}; filename="{asset["original_filename"]}"'
                    )
                },
            )

        if project_id and filename:
            for candidate in active_store.list_assets(project_id):
                if filename in {
                    candidate.get("filename"),
                    candidate.get("original_filename"),
                    (candidate.get("metadata") or {}).get("comfy_filename"),
                }:
                    cpath = active_store.get_asset_path(project_id, candidate["id"])
                    if cpath is not None:
                        return Response(
                            content=cpath.read_bytes(),
                            media_type=candidate["mime_type"],
                            headers={
                                "Content-Disposition": (
                                    f'{disposition}; filename="{candidate["original_filename"]}"'
                                )
                            },
                        )

        if filename:
            try:
                raw_bytes = await active_comfyui.view_output(
                    filename,
                    subfolder=subfolder,
                    storage_type=type,
                )
            except ComfyUIError as exc:
                raise HTTPException(
                    status_code=exc.status_code or 404,
                    detail=exc.message,
                ) from exc
            media_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/png"
            safe_name = os.path.basename(filename) or "output.bin"
            return Response(
                content=raw_bytes,
                media_type=media_type,
                headers={
                    "Content-Disposition": f'{disposition}; filename="{safe_name}"'
                },
            )

        raise HTTPException(
            status_code=400,
            detail="Debe especificar 'project_id' + 'asset_id' o 'filename'.",
        )

    # --- Registro de plantillas (list / get / submit) ---

    @app.get("/studio/api/templates")
    async def list_templates_endpoint(
        request: Request,
        include_workflow: bool = Query(default=False),
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        return {
            "templates": active_registry.list_templates(
                include_workflow=include_workflow
            )
        }

    @app.get("/studio/api/templates/{template_id}")
    async def get_template_endpoint(
        template_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_registry: TemplateRegistry = request.app.state.template_registry
        try:
            return active_registry.get_template(template_id, include_workflow=True)
        except TemplateNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/studio/api/templates/{template_id}/submit")
    @app.post("/studio/api/projects/{project_id}/templates/{template_id}/submit")
    async def submit_template_endpoint(
        template_id: str,
        request: Request,
        project_id: str | None = None,
    ):
        active_registry: TemplateRegistry = request.app.state.template_registry
        active_store: ProjectStore = request.app.state.store
        active_comfyui: ComfyUIClient = request.app.state.comfyui_client
        active_broker: ProgressBroker = request.app.state.broker
        active_redactor = request.app.state.config.redactor()

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
            result = await active_registry.submit_render(
                template_id,
                raw_params,
                project_id=eff_project_id,
                store=active_store,
                comfyui_client=active_comfyui,
                broker=active_broker,
                strict=True,
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

        return active_redactor.redact_value(result)

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

    @app.get("/studio/api/agent/tools")
    @app.get("/studio/api/tools")
    async def list_agent_tools(request: Request) -> dict[str, Any]:
        active_agent: StudioAgent = request.app.state.agent
        return {"tools": active_agent.tool_belt.to_openai_tools()}

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

        wants_stream = body.stream or "text/event-stream" in (
            request.headers.get("accept") or ""
        )
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
                except ValueError as exc:
                    err_ev = active_redactor.redact_value(
                        {"type": "error", "detail": str(exc)}
                    )
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
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        return {
            "project_id": project_id,
            "messages": active_store.list_chat_messages(project_id, limit=limit),
        }

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

    @app.get("/studio/api/projects/{project_id}/edl")
    async def list_project_edls(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        latest = active_store.get_latest_edl(project_id)
        return {
            "project_id": project_id,
            "versions": active_store.list_edl_versions(project_id),
            "latest": latest,
            "edl": latest,
        }

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

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.api_route(
        "/studio/api/{unknown_path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def unknown_api_route(unknown_path: str) -> JSONResponse:
        raise HTTPException(
            status_code=404,
            detail=f"Ruta de API no encontrada: /studio/api/{unknown_path}",
        )

    panel_dir = _resolve_panel_dir()
    if panel_dir is not None:
        index_file = panel_dir / "index.html"

        @app.get("/studio", include_in_schema=False)
        async def serve_studio_root_no_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        @app.get("/studio/", include_in_schema=False)
        async def serve_studio_root_slash() -> FileResponse:
            return FileResponse(index_file, media_type="text/html; charset=utf-8")

        app.mount(
            "/studio",
            StaticFiles(directory=str(panel_dir), html=True),
            name="studio-panel",
        )

    return app


def _resolve_panel_dir() -> Path | None:
    """Localiza el directorio estático panel/ en entorno local o contenedor."""
    candidates: list[Path] = []
    env_dir = os.environ.get("STUDIO_PANEL_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir))
    candidates.extend(
        [
            Path(__file__).resolve().parents[2] / "panel",
            Path("/etc/comfyui/panel"),
            Path("/opt/studio/panel"),
            Path.cwd() / "panel",
        ]
    )
    for candidate in candidates:
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


app = create_app()
