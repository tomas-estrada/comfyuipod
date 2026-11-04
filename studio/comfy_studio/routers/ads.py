"""Estudio de anuncios (motor ad_studio): brief -> guion -> storyboard -> variantes.

Rutas nativas del paquete de anuncios (M7, ver
docs/propuestas/INVESTIGACION_COMPETITIVA_2026-11-04.md). `variants` crea un job
`ad_variants` y corre en segundo plano (202 + job_id, como las plantillas);
`?wait=1` lo ejecuta en línea. El progreso llega por SSE y el job admite
cancelación (sticky) entre variantes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from comfy_studio.ads.pipeline import (
    AdCancelledError,
    AdPipelineError,
    attach_storyboard,
    create_ad_doc,
    get_ad_doc,
    list_ad_docs,
    run_ad_variants,
)
from comfy_studio.ads.schema import AdBrief
from comfy_studio.engines import is_stub_mode
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    AdBriefRequest,
    AdVariantsRequest,
)

logger = logging.getLogger("comfy_studio.routers.ads")


async def _run_ad_job_task(app: FastAPI, job_id: str) -> None:
    """Ejecuta en segundo plano el job `ad_variants` creado por el endpoint."""
    store: ProjectStore = app.state.store
    broker: ProgressBroker = app.state.broker
    redactor = app.state.config.redactor()
    job = store.get_job(job_id)
    if job is None:
        return
    params = job.get("params") or {}
    project_id = str(params.get("project_id") or job.get("project_id") or "")
    ad_id = str(params.get("ad_id") or "")
    try:
        await run_in_threadpool(
            run_ad_variants,
            store,
            project_id,
            ad_id,
            broker=broker,
            config=app.state.config,
            job_id=job_id,
            aspect_ratios=params.get("aspect_ratios") or None,
            voiceover=bool(params.get("voiceover", False)),
            voice_id=str(params.get("voice_id", "default")),
            music=bool(params.get("music", False)),
            music_prompt=str(params.get("music_prompt", "")),
            music_duration_s=float(params.get("music_duration_s", 0.0) or 0.0),
            source_asset_id=params.get("source_asset_id"),
            chunk_asset_ids=params.get("chunk_asset_ids") or None,
            fps=int(params.get("fps", 24)),
            max_retries=int(params.get("max_retries", 0)),
            acceptance=params.get("acceptance") or None,
            simulated=is_stub_mode("ad_studio"),
        )
    except AdCancelledError as exc:
        store.update_job(job_id, status="cancelled", error=str(exc))
        broker.publish(
            "job",
            {
                "job_id": job_id,
                "project_id": project_id,
                "kind": "ad_variants",
                "status": "cancelled",
            },
        )
    except AdPipelineError as exc:
        store.update_job(job_id, status="failed", error=str(exc))
        broker.publish(
            "job",
            {"job_id": job_id, "project_id": project_id, "kind": "ad_variants", "status": "failed",
             "error": redactor.redact(str(exc))},
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # nunca tumbar el proceso por un job
        logger.exception("Job de variantes de anuncio %s falló", job_id)
        store.update_job(job_id, status="failed", error=redactor.redact(str(exc)))
        broker.publish(
            "job",
            {"job_id": job_id, "project_id": project_id, "kind": "ad_variants", "status": "failed",
             "error": redactor.redact(str(exc))},
        )


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app`."""

    @app.post("/studio/api/projects/{project_id}/ads/script", status_code=201)
    async def create_ad_script_endpoint(
        project_id: str,
        body: AdBriefRequest,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            brief = AdBrief.model_validate(body.model_dump())
            doc = await run_in_threadpool(create_ad_doc, store, project_id, brief)
        except (ValueError, AdPipelineError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return doc

    @app.get("/studio/api/projects/{project_id}/ads")
    async def list_ads_endpoint(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        docs = await run_in_threadpool(list_ad_docs, store, project_id)
        return {"project_id": project_id, "count": len(docs), "ads": docs}

    @app.get("/studio/api/projects/{project_id}/ads/{ad_id}")
    async def get_ad_endpoint(
        project_id: str,
        ad_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        doc = await run_in_threadpool(get_ad_doc, store, project_id, ad_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="Anuncio no encontrado")
        return doc

    @app.post("/studio/api/projects/{project_id}/ads/{ad_id}/storyboard")
    async def build_ad_storyboard_endpoint(
        project_id: str,
        ad_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store: ProjectStore = request.app.state.store
        broker: ProgressBroker = request.app.state.broker
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            doc = await run_in_threadpool(attach_storyboard, store, project_id, ad_id)
        except AdPipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        broker.publish(
            "ad",
            {
                "project_id": project_id,
                "ad_id": ad_id,
                "event": "storyboard",
                "shots": len((doc.get("storyboard") or {}).get("shots") or []),
                "gate": doc.get("gate"),
            },
        )
        return doc

    @app.post("/studio/api/projects/{project_id}/ads/{ad_id}/variants")
    async def build_ad_variants_endpoint(
        project_id: str,
        ad_id: str,
        request: Request,
        body: AdVariantsRequest | None = None,
        wait: bool = Query(default=False),
    ):
        store: ProjectStore = request.app.state.store
        broker: ProgressBroker = request.app.state.broker
        redactor = request.app.state.config.redactor()
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        req = body or AdVariantsRequest()
        if get_ad_doc(store, project_id, ad_id) is None:
            raise HTTPException(status_code=404, detail="Anuncio no encontrado")

        params = {**req.model_dump(), "project_id": project_id, "ad_id": ad_id}
        job = store.create_job(
            kind="ad_variants",
            project_id=project_id,
            params=params,
        )
        broker.publish(
            "job",
            {
                "job_id": job["id"],
                "project_id": project_id,
                "kind": job["kind"],
                "status": job["status"],
                "ad_id": ad_id,
            },
        )

        if wait:
            try:
                summary = await run_in_threadpool(
                    run_ad_variants,
                    store,
                    project_id,
                    ad_id,
                    broker=broker,
                    config=request.app.state.config,
                    job_id=job["id"],
                    aspect_ratios=req.aspect_ratios,
                    voiceover=req.voiceover,
                    voice_id=req.voice_id,
                    music=req.music,
                    music_prompt=req.music_prompt,
                    music_duration_s=req.music_duration_s,
                    source_asset_id=req.source_asset_id,
                    chunk_asset_ids=req.chunk_asset_ids or None,
                    fps=req.fps,
                    max_retries=req.max_retries,
                    acceptance=req.acceptance or None,
                    simulated=is_stub_mode("ad_studio"),
                )
            except AdCancelledError as exc:
                store.update_job(job["id"], status="cancelled", error=str(exc))
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except AdPipelineError as exc:
                store.update_job(job["id"], status="failed", error=str(exc))
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return redactor.redact_value(summary)

        task = asyncio.create_task(_run_ad_job_task(request.app, job["id"]))
        tasks = getattr(request.app.state, "ad_tasks", None)
        if tasks is not None:
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        return JSONResponse(
            status_code=202,
            content=redactor.redact_value(
                {
                    "status": "queued",
                    "async": True,
                    "job_id": job["id"],
                    "project_id": project_id,
                    "ad_id": ad_id,
                    "location": f"/studio/api/jobs/{job['id']}",
                    "progress_url": "/studio/api/progress",
                }
            ),
        )
