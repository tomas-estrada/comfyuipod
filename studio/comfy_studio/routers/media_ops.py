"""ASR/diarización, subtítulos, EDL y SSE de progreso. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from comfy_studio.asr import diarize_project_asset, transcribe_project_asset
from comfy_studio.config import (
    StudioConfig,
)
from comfy_studio.edl import execute_edl_ffmpeg, generate_highlights_edl
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.subtitles import render_subtitles, subtitle_filename
from comfy_studio.webutil import (
    DiarizeRequest,
    EdlBuildRequest,
    EdlCreateRequest,
    EdlHighlightsRequest,
    TranscribeRequest,
    _publish_transcription_event,
)


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/projects/{project_id}/transcriptions")
    async def list_project_transcriptions(
        project_id: str,
        request: Request,
        asset_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        items = active_store.list_transcriptions(project_id, asset_id=asset_id)
        latest = items[0] if items else None
        return {
            "project_id": project_id,
            "count": len(items),
            "latest": latest,
            "transcriptions": items,
        }
    @app.get("/studio/api/projects/{project_id}/transcriptions/{transcription_id}/subtitles")
    async def export_project_subtitles(
        project_id: str,
        transcription_id: str,
        request: Request,
        fmt: str = Query(default="srt", pattern="^(srt|vtt)$"),
        include_speakers: bool = Query(default=True),
        download: bool = Query(default=False),
    ) -> Response:
        """Exporta la transcripción como SRT o WebVTT (M2, `M3-IDEA-032`).

        Es formateo puro sobre datos ya persistidos: funciona igual con la transcripción
        simulada actual y con la real del hito M3.
        """
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        transcription = active_store.get_transcription(project_id, transcription_id)
        if transcription is None:
            raise HTTPException(status_code=404, detail="Transcripción no encontrada")
        try:
            body = render_subtitles(
                transcription,
                fmt=fmt,  # type: ignore[arg-type]
                include_speakers=include_speakers,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        media_type = "application/x-subrip" if fmt == "srt" else "text/vtt"
        headers = {"Cache-Control": "no-store"}
        if download:
            filename = subtitle_filename(transcription, fmt=fmt)  # type: ignore[arg-type]
            headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return Response(
            content=body,
            media_type=f"{media_type}; charset=utf-8",
            headers=headers,
        )
    @app.post("/studio/api/projects/{project_id}/transcribe")
    async def transcribe_project_endpoint(
        project_id: str,
        body: TranscribeRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            result = await run_in_threadpool(
                transcribe_project_asset,
                active_store,
                project_id,
                asset_id=body.asset_id,
                language=body.language,
                script_segments=body.script_segments,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish_transcription_event(active_broker, project_id, result)
        return {
            "status": "transcribed",
            "project_id": project_id,
            "transcription": result,
        }
    @app.post("/studio/api/projects/{project_id}/diarize")
    async def diarize_project_endpoint(
        project_id: str,
        body: DiarizeRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            result = await run_in_threadpool(
                diarize_project_asset,
                active_store,
                project_id,
                asset_id=body.asset_id,
                transcription_id=body.transcription_id,
                expected_speakers=body.expected_speakers,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if isinstance(result.get("transcription"), dict):
            _publish_transcription_event(active_broker, project_id, result["transcription"])
        return {
            "status": "diarized",
            "project_id": project_id,
            **result,
        }
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
    @app.post("/studio/api/projects/{project_id}/edl", status_code=201)
    async def save_project_edl_endpoint(
        project_id: str,
        body: EdlCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        cuts_dicts = [c.model_dump(exclude_none=True) for c in body.cuts]
        total_dur = round(sum(c.out_sec - c.in_sec for c in body.cuts), 3)
        edl_doc: dict[str, Any] = {
            "schema": "comfy_studio.edl.v1",
            "title": body.title,
            "aspect_ratio": body.aspect_ratio,
            "source_asset_id": body.source_asset_id,
            "total_duration_sec": total_dur,
            "cuts": cuts_dicts,
            "notes": body.notes,
        }
        saved = active_store.save_edl_version(project_id, edl=edl_doc)
        active_broker.publish(
            "edl",
            {
                "project_id": project_id,
                "edl_id": saved["id"],
                "version": saved["version"],
                "aspect_ratio": body.aspect_ratio,
                "total_duration_sec": total_dur,
                "cuts_count": len(cuts_dicts),
                # M2: `status` distingue propuesta de render en el evento webhook.
                "status": "proposed",
            },
        )
        return saved
    @app.post("/studio/api/projects/{project_id}/edl/highlights")
    async def generate_project_edl_highlights_endpoint(
        project_id: str,
        body: EdlHighlightsRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            saved = await run_in_threadpool(
                generate_highlights_edl,
                active_store,
                project_id,
                asset_id=body.asset_id,
                transcription_id=body.transcription_id,
                title=body.title,
                aspect_ratio=body.aspect_ratio,  # type: ignore[arg-type]
                remove_fillers=body.remove_fillers,
                min_pause_sec=body.min_pause_sec,
                max_duration_sec=body.max_duration_sec,
                speaker_filter=body.speaker_filter,
                hybrid=body.hybrid,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        active_broker.publish(
            "edl",
            {
                "project_id": project_id,
                "edl_id": saved["id"],
                "version": saved["version"],
                "aspect_ratio": body.aspect_ratio,
                "total_duration_sec": saved["edl"].get("total_duration_sec", 0.0),
                "cuts_count": len(saved["edl"].get("cuts") or []),
                "status": "proposed",
            },
        )
        return saved
    @app.post("/studio/api/projects/{project_id}/edl/build")
    async def build_project_edl_endpoint(
        project_id: str,
        body: EdlBuildRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        cuts_dicts = (
            [c.model_dump(exclude_none=True) for c in body.cuts] if body.cuts is not None else None
        )

        async def _render_one(aspect: str | None) -> dict[str, Any]:
            try:
                result = await run_in_threadpool(
                    execute_edl_ffmpeg,
                    active_store,
                    project_id,
                    edl_id=body.edl_id,
                    version=body.version,
                    aspect_ratio=aspect,
                    resolution=body.resolution,
                    source_asset_id=body.source_asset_id,
                    cuts=cuts_dicts,
                    fps=body.fps,
                    ffmpeg_timeout_s=active_cfg.studio_ffmpeg_timeout_s,
                    ffprobe_timeout_s=active_cfg.studio_ffprobe_timeout_s,
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Un evento webhook/SSE por variante (M2, `M3-IDEA-036`).
            active_broker.publish(
                "edl",
                {
                    "project_id": project_id,
                    "edl_id": result["edl_id"],
                    "version": result["version"],
                    "aspect_ratio": result["aspect_ratio"],
                    "output_asset_id": result["output_asset"]["id"],
                    "status": "built",
                },
            )
            return result

        variants = body.resolved_aspect_ratios()
        if not variants:
            return await _render_one(body.aspect_ratio)

        # Render por lotes: secuencial a propósito. Cada EDL ya lanza decenas de
        # subprocess ffmpeg; paralelizarlos competiría por CPU con ComfyUI y haría
        # impredecible el tiempo total. El resultado expone una entrada por variante.
        results: list[dict[str, Any]] = []
        for aspect in variants:
            results.append(await _render_one(aspect))
        first = results[0]
        return {
            **first,
            "variants": results,
            "variant_count": len(results),
            "aspect_ratios": [r["aspect_ratio"] for r in results],
        }
    @app.post("/studio/api/projects/{project_id}/edl/{version}/render")
    async def render_project_edl_version_endpoint(
        project_id: str,
        version: int,
        request: Request,
        body: EdlBuildRequest | None = None,
    ) -> dict[str, Any]:
        req_body = body or EdlBuildRequest()
        req_body.version = version
        return await build_project_edl_endpoint(
            project_id=project_id,
            body=req_body,
            request=request,
        )

    @app.post("/studio/api/projects/{project_id}/edl/{version}/editable-package")
    async def export_editable_edl_package(
        project_id: str,
        version: int,
        request: Request,
    ) -> FileResponse:
        """Descarga un proyecto editable con fuentes incluidas desde un EDL guardado."""
        from comfy_studio.delivery_4k import (
            ASPECT_4K,
            Clip,
            TimelineSpec,
            write_editable_package_archive,
        )
        from comfy_studio.perception.keyframes import probe_media_info

        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        record = active_store.get_edl_version(project_id, version=version)
        if record is None:
            raise HTTPException(status_code=404, detail="Versión de EDL no encontrada")
        edl = record.get("edl") or {}
        aspect = str(edl.get("aspect_ratio") or "16:9")
        if aspect not in ASPECT_4K:
            raise HTTPException(
                status_code=400,
                detail=f"No hay perfil 4K editable para el aspecto {aspect!r}",
            )
        cuts = edl.get("cuts")
        if not isinstance(cuts, list) or not cuts:
            raise HTTPException(status_code=400, detail="El EDL no contiene cortes")

        clips: list[Clip] = []
        dimensions: dict[str, tuple[int, int]] = {}
        for index, cut in enumerate(cuts, start=1):
            if not isinstance(cut, dict):
                raise HTTPException(status_code=400, detail="Corte de EDL inválido")
            asset_id = str(cut.get("asset_id") or edl.get("source_asset_id") or "").strip()
            asset = active_store.get_asset(project_id, asset_id) if asset_id else None
            source = active_store.get_asset_path(project_id, asset_id) if asset else None
            if asset is None or source is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se puede resolver el medio fuente del corte {index}",
                )
            if asset_id not in dimensions:
                info = probe_media_info(
                    source,
                    timeout_s=request.app.state.config.studio_ffprobe_timeout_s,
                )
                if not info.get("probe_ok"):
                    raise HTTPException(
                        status_code=400,
                        detail=f"No se pudieron verificar las dimensiones del medio {asset_id}",
                    )
                dimensions[asset_id] = (int(info["width"]), int(info["height"]))
            try:
                start, end = float(cut["in_sec"]), float(cut["out_sec"])
                if start < 0 or end <= start:
                    raise ValueError("rango inválido")
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"Rango inválido en el corte {index}"
                ) from exc
            metadata = asset.get("metadata") or {}
            clips.append(
                Clip(
                    clip_id=f"cut-{index:04d}",
                    source_path=str(source),
                    in_s=start,
                    out_s=end,
                    license=str(metadata.get("license") or "unknown"),
                    src_width=dimensions[asset_id][0],
                    src_height=dimensions[asset_id][1],
                    source_asset_id=asset_id,
                    reason=str(cut.get("reason") or "cut"),
                )
            )

        timeline = TimelineSpec(
            name=str(edl.get("title") or f"EDL v{version}"),
            aspect=aspect,
            fps=24,
            clips=clips,
        )
        fd, archive_name = tempfile.mkstemp(prefix="comfyuipod-editable-", suffix=".zip")
        os.close(fd)
        archive = await run_in_threadpool(write_editable_package_archive, timeline, archive_name)
        return FileResponse(
            archive,
            media_type="application/zip",
            filename="comfyuipod-editable-delivery.zip",
            background=BackgroundTask(lambda: archive.unlink(missing_ok=True)),
        )
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
