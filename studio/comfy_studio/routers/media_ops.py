"""ASR/diarización, subtítulos, EDL y SSE de progreso. (extraído de app.py, M7-SCR-002)"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
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
from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.editor import EditorEngine
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.subtitles import render_subtitles, subtitle_filename
from comfy_studio.webutil import (
    DiarizeRequest,
    EdlBuildRequest,
    EdlCreateRequest,
    EdlHighlightsRequest,
    EdlProgrammedRenderRequest,
    ReelRenderRequest,
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
        total_dur_raw = 0.0
        previous_duration = 0.0
        for index, cut in enumerate(body.cuts):
            duration = cut.out_sec - cut.in_sec
            overlap = (
                min(cut.transition_duration_s, duration, previous_duration)
                if index > 0 and cut.transition_in in {"crossfade", "slide"}
                else 0.0
            )
            total_dur_raw += duration - overlap
            previous_duration = duration
        total_dur = round(total_dur_raw, 3)
        if any(item.end_s > total_dur_raw for item in [*body.titles, *body.captions]):
            raise HTTPException(
                status_code=422,
                detail="El título o caption no puede superar la duración final del EDL",
            )
        audio_asset_ids = list(dict.fromkeys(
            [*(body.audio_asset_ids or []), *([body.audio_asset_id] if body.audio_asset_id else [])]
        ))
        for audio_id in audio_asset_ids:
            audio = active_store.get_asset(project_id, audio_id)
            audio_path = active_store.get_asset_path(project_id, audio_id)
            if audio is None or audio.get("kind") != "audio" or audio_path is None:
                raise HTTPException(
                    status_code=400, detail=f"La pista de audio EDL {audio_id!r} no está disponible"
                )
        edl_doc: dict[str, Any] = {
            "schema": "comfy_studio.edl.v1",
            "title": body.title,
            "aspect_ratio": body.aspect_ratio,
            "source_asset_id": body.source_asset_id,
            "total_duration_sec": total_dur,
            "cuts": cuts_dicts,
            "titles": [item.model_dump() for item in body.titles],
            "captions": [item.model_dump() for item in body.captions],
            "audio_asset_id": body.audio_asset_id,
            "audio_asset_ids": audio_asset_ids,
            "fps": body.fps,
            "color_space": body.color_space,
            "color_range": body.color_range,
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
                    color_range=body.color_range,
                    picture_inset_percent=body.picture_inset_percent,
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

    @app.post("/studio/api/projects/{project_id}/edl/{version}/programmed-render", status_code=201)
    async def render_programmed_edl_version(
        project_id: str,
        version: int,
        body: EdlProgrammedRenderRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Renderiza con Remotion una versión EDL editada y persistida."""
        from comfy_studio.engines.editor import Clip, TextOverlay
        from comfy_studio.perception.keyframes import probe_media_info
        from comfy_studio.projects import MediaValidationError
        from comfy_studio.reel_presets import resolve_reel_preset

        store: ProjectStore = request.app.state.store
        config: StudioConfig = request.app.state.config
        if store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        record = store.get_edl_version(project_id, version=version)
        if record is None:
            raise HTTPException(status_code=404, detail="Versión de EDL no encontrada")
        edl = record.get("edl") or {}
        cuts = edl.get("cuts")
        if not isinstance(cuts, list) or not cuts:
            raise HTTPException(status_code=400, detail="El EDL no contiene cortes")
        if len(cuts) > 50:
            raise HTTPException(
                status_code=422, detail="Remotion admite hasta 50 cortes por preset"
            )

        clips: list[Clip] = []
        source_ids: list[str] = []
        for index, cut in enumerate(cuts, start=1):
            if not isinstance(cut, dict):
                raise HTTPException(status_code=400, detail=f"Corte #{index} inválido")
            asset_id = str(cut.get("asset_id") or edl.get("source_asset_id") or "").strip()
            asset = store.get_asset(project_id, asset_id) if asset_id else None
            source = store.get_asset_path(project_id, asset_id) if asset else None
            try:
                start_s, end_s = float(cut["in_sec"]), float(cut["out_sec"])
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=400, detail=f"Rango inválido en el corte #{index}"
                ) from exc
            if asset is None or asset.get("kind") != "video" or source is None:
                raise HTTPException(
                    status_code=400, detail=f"El corte #{index} no tiene vídeo fuente del proyecto"
                )
            if start_s < 0 or end_s <= start_s:
                raise HTTPException(status_code=400, detail=f"Rango inválido en el corte #{index}")
            info = probe_media_info(source, timeout_s=config.studio_ffprobe_timeout_s)
            duration = end_s - start_s
            if (
                not info.get("probe_ok")
                or not info.get("has_video")
                or end_s > float(info.get("duration_sec") or 0) + 0.05
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"El corte #{index} excede o no verifica el vídeo fuente",
                )
            clips.append(
                Clip(
                    path=source,
                    duration_s=duration,
                    start_s=start_s,
                    transition_in=str(cut.get("transition_in") or "cut"),
                    transition_duration_s=float(cut.get("transition_duration_s", 0.3)),
                )
            )
            source_ids.append(asset_id)

        total_s = 0.0
        previous_duration = 0.0
        for index, clip in enumerate(clips):
            overlap = (
                min(clip.transition_duration_s, clip.duration_s, previous_duration)
                if index and clip.transition_in in {"crossfade", "slide"}
                else 0.0
            )
            total_s += clip.duration_s - overlap
            previous_duration = clip.duration_s
        target_color_space = body.color_space or str(edl.get("color_space") or "bt709")
        target_color_range = body.color_range or str(edl.get("color_range") or "limited")
        try:
            preset, width, height, fps = resolve_reel_preset(
                body.preset_id,
                width=body.width,
                height=body.height,
                fps=body.fps or int(edl.get("fps") or 24),
                duration_s=total_s,
                clip_count=len(clips),
            )
            titles = [TextOverlay(**item) for item in edl.get("titles", [])]
            captions = [TextOverlay(**item) for item in edl.get("captions", [])]
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"EDL programado inválido: {exc}") from exc
        if any(item.end_s > total_s for item in [*titles, *captions]):
            raise HTTPException(
                status_code=422, detail="Un título o caption supera la duración final"
            )

        audio_ids = list(dict.fromkeys(
            [
                str(value).strip()
                for value in edl.get("audio_asset_ids", [])
                if str(value).strip()
            ]
            + ([str(edl.get("audio_asset_id") or "").strip()]
               if str(edl.get("audio_asset_id") or "").strip() else [])
        ))
        audio_paths: list[Path] = []
        audio_gains_db: list[float] = []
        for audio_id in audio_ids:
            audio_path = store.get_asset_path(project_id, audio_id)
            audio_asset = store.get_asset(project_id, audio_id)
            if audio_asset is None or audio_asset.get("kind") != "audio" or audio_path is None:
                raise HTTPException(
                    status_code=400, detail=f"La pista de audio EDL {audio_id!r} no está disponible"
                )
            audio_paths.append(audio_path)
            try:
                gain_db = float((audio_asset.get("metadata") or {}).get("gain_db", 0.0))
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Ganancia de audio inválida") from exc
            if not -60 <= gain_db <= 12:
                raise HTTPException(status_code=400, detail="Ganancia de audio fuera de rango")
            audio_gains_db.append(gain_db)

        with tempfile.TemporaryDirectory(prefix="studio-edl-remotion-") as output_dir:
            output_path = Path(output_dir) / "edl-reel.mp4"
            job = {
                "clips": [
                    {
                        "path": str(item.path),
                        "start_s": item.start_s,
                        "duration_s": item.duration_s,
                        "transition_in": item.transition_in,
                        "transition_duration_s": item.transition_duration_s,
                    }
                    for item in clips
                ],
                "output_path": str(output_path),
                "width": width,
                "height": height,
                "fps": fps,
                "audio_paths": [str(path) for path in audio_paths],
                "audio_gains_db": audio_gains_db,
                "safe_area_percent": preset.safe_area_percent,
                "color_space": target_color_space,
                "color_range": target_color_range,
                "titles": [
                    {
                        "text": item.text,
                        "start_s": item.start_s,
                        "end_s": item.end_s,
                        "position": item.position,
                    }
                    for item in titles
                ],
                "captions": [
                    {
                        "text": item.text,
                        "start_s": item.start_s,
                        "end_s": item.end_s,
                        "position": item.position,
                    }
                    for item in captions
                ],
                "render_concurrency": preset.concurrency,
                "offthread_cache_bytes": preset.offthread_cache_bytes,
                "memory_efficient": True,
            }
            result = await run_in_threadpool(
                EditorEngine(backend="remotion").run,
                EngineRunSpec(job=job, timeout_s=config.studio_ffmpeg_timeout_s),
            )
            rendered = Path(str(result.payload.get("output") or ""))
            if not result.ok or result.simulated or not rendered.is_file():
                raise HTTPException(
                    status_code=503, detail="Falló Remotion; no se publicó una salida simulada"
                )
            output_info = probe_media_info(rendered, timeout_s=config.studio_ffprobe_timeout_s)
            if (
                not output_info.get("probe_ok")
                or not output_info.get("has_video")
                or int(output_info.get("width") or 0) != width
                or int(output_info.get("height") or 0) != height
                or abs(float(output_info.get("fps") or 0.0) - fps) > 0.01
                or output_info.get("color_range")
                != ("pc" if target_color_range == "full" else "tv")
                or output_info.get("color_space") != target_color_space
                or output_info.get("color_transfer") != target_color_space
                or output_info.get("color_primaries") != target_color_space
                or (audio_ids and not output_info.get("has_audio"))
                or abs(float(output_info.get("duration_sec") or 0) - total_s) > (1 / fps + 0.05)
            ):
                raise HTTPException(
                    status_code=503, detail="La salida Remotion no verificó dimensiones/duración"
                )
            audio_loudness = result.payload.get("audio_loudness_lufs")
            true_peak = result.payload.get("true_peak_dbfs")
            if audio_ids and (
                audio_loudness is None
                or true_peak is None
                or abs(float(audio_loudness) + 16.0) > 1.0
                or float(true_peak) > -0.5
            ):
                raise HTTPException(
                    status_code=503,
                    detail="El máster no verificó loudness objetivo (-16 LUFS / pico ≤ -1 dBFS)",
                )
            if rendered.stat().st_size > config.max_upload_bytes:
                raise HTTPException(status_code=413, detail="El reel renderizado supera el límite")
            try:
                output_asset = store.save_upload(
                    project_id,
                    filename=f"edl-v{version}-programmed.mp4",
                    content_type="video/mp4",
                    data=rendered.read_bytes(),
                    max_upload_bytes=config.max_upload_bytes,
                    category="outputs",
                    metadata={
                        "engine": "remotion",
                        "edl_version": version,
                        "preset_id": preset.preset_id,
                        "source_asset_ids": source_ids,
                        "audio_asset_id": audio_ids[0] if audio_ids else None,
                        "audio_asset_ids": audio_ids,
                        "width": width,
                        "height": height,
                        "fps": fps,
                        "color_space": target_color_space,
                        "color_range": target_color_range,
                        "safe_area_percent": preset.safe_area_percent,
                        "audio_loudness_lufs": audio_loudness,
                        "true_peak_dbfs": true_peak,
                        "duration_s": round(total_s, 3),
                    },
                )
            except (LookupError, MediaValidationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "rendered",
            "project_id": project_id,
            "edl_version": version,
            "engine": "remotion",
            "audio_asset_ids": audio_ids,
            "delivery_policy": {
                "width": width,
                "height": height,
                "fps": fps,
                "color_space": target_color_space,
                "color_range": target_color_range,
                "safe_area_percent": preset.safe_area_percent,
                "audio_loudness_lufs": audio_loudness,
                "true_peak_dbfs": true_peak,
            },
            "simulated": False,
            "output_asset": output_asset,
        }

    @app.post("/studio/api/projects/{project_id}/edl/{version}/editable-package")
    async def export_editable_edl_package(
        project_id: str,
        version: int,
        request: Request,
        body: EdlProgrammedRenderRequest | None = None,
    ) -> FileResponse:
        """Renderiza un máster 4K real y lo incluye con las fuentes en el proyecto editable."""
        from comfy_studio.delivery_4k import (
            ASPECT_4K,
            AudioStem,
            Clip,
            DeliveryError,
            TimelineSpec,
            Title,
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
            if asset is None or source is None or asset.get("kind") != "video":
                raise HTTPException(
                    status_code=400,
                    detail=f"El corte {index} requiere un vídeo fuente del proyecto",
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

        titles = [
            Title(
                title_id=f"title-{index:03d}",
                text=str(item.get("text") or ""),
                start_s=float(item.get("start_s", -1)),
                end_s=float(item.get("end_s", 0)),
                kind="title",
                position=str(item.get("position") or "bottom"),
            )
            for index, item in enumerate(edl.get("titles", []), start=1)
        ]
        captions = [
            Title(
                title_id=f"caption-{index:03d}",
                text=str(item.get("text") or ""),
                start_s=float(item.get("start_s", -1)),
                end_s=float(item.get("end_s", 0)),
                kind="caption",
                position=str(item.get("position") or "bottom"),
            )
            for index, item in enumerate(edl.get("captions", []), start=1)
        ]

        audio_stems: list[AudioStem] = []
        audio_ids = list(dict.fromkeys(
            [
                str(value).strip()
                for value in edl.get("audio_asset_ids", [])
                if str(value).strip()
            ]
            + ([str(edl.get("audio_asset_id") or "").strip()]
               if str(edl.get("audio_asset_id") or "").strip() else [])
        ))
        for index, audio_id in enumerate(audio_ids, start=1):
            audio_asset = active_store.get_asset(project_id, audio_id)
            audio_source = active_store.get_asset_path(project_id, audio_id)
            if audio_asset is None or audio_asset.get("kind") != "audio" or audio_source is None:
                raise HTTPException(
                    status_code=400, detail=f"La pista de audio EDL {audio_id!r} no está disponible"
                )
            audio_info = probe_media_info(
                audio_source,
                timeout_s=request.app.state.config.studio_ffprobe_timeout_s,
            )
            if not audio_info.get("probe_ok") or not audio_info.get("has_audio"):
                raise HTTPException(status_code=400, detail="No se pudo verificar el audio fuente")
            audio_meta = audio_asset.get("metadata") or {}
            try:
                gain_db = float(audio_meta.get("gain_db") or 0.0)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Ganancia de audio inválida") from exc
            if not -60 <= gain_db <= 12:
                raise HTTPException(status_code=400, detail="Ganancia de audio fuera de rango")
            audio_stems.append(
                AudioStem(
                    stem_id=f"edl-audio-{index:02d}",
                    source_path=str(audio_source),
                    role=str(audio_meta.get("role") or ("mix" if index == 1 else "stem")),
                    license=str(audio_meta.get("license") or "unknown"),
                    gain_db=gain_db,
                    channels=max(1, min(8, int(audio_info.get("audio_channels") or 2))),
                )
            )

        transitions = [str(cut.get("transition_in") or "cut") for cut in cuts]

        fps = int(edl.get("fps") or 24)
        color_space = str(edl.get("color_space") or "bt709")
        color_range = str(edl.get("color_range") or "limited")
        safe_area_percent = 10.0 if aspect == "9:16" else 5.0
        timeline = TimelineSpec(
            name=str(edl.get("title") or f"EDL v{version}"),
            aspect=aspect,
            fps=fps,
            clips=clips,
            titles=titles + captions,
            transitions=transitions,
            audio_stems=audio_stems,
            color_space=color_space,
            color_range=color_range,
            safe_area_percent=safe_area_percent,
        )
        preset_id = {
            "16:9": "delivery-landscape-4k-v1",
            "9:16": "delivery-vertical-4k-v1",
            "1:1": "delivery-square-4k-v1",
        }[aspect]
        render_request = body or EdlProgrammedRenderRequest(preset_id=preset_id)
        if (
            render_request.preset_id != preset_id
            or render_request.width is not None
            or render_request.height is not None
        ):
            raise HTTPException(
                status_code=422,
                detail=f"El paquete editable requiere el preset 4K {preset_id}",
            )
        try:
            rendered_result = await render_programmed_edl_version(
                project_id, version, render_request, request
            )
        except HTTPException:
            raise
        master_asset = rendered_result["output_asset"]
        delivery_policy = rendered_result["delivery_policy"]
        timeline.fps = int(delivery_policy["fps"])
        timeline.color_space = str(delivery_policy["color_space"])
        timeline.color_range = str(delivery_policy["color_range"])
        timeline.safe_area_percent = float(delivery_policy["safe_area_percent"])
        timeline.audio_loudness_lufs = delivery_policy["audio_loudness_lufs"]
        timeline.true_peak_dbfs = delivery_policy["true_peak_dbfs"]
        master_path = active_store.get_asset_path(project_id, str(master_asset["id"]))
        if master_path is None:
            raise HTTPException(status_code=503, detail="El máster renderizado no está disponible")
        fd, archive_name = tempfile.mkstemp(prefix="comfyuipod-editable-", suffix=".zip")
        os.close(fd)
        try:
            archive = await run_in_threadpool(
                write_editable_package_archive,
                timeline,
                archive_name,
                additional_artifacts=[
                    {
                        "source_path": str(master_path),
                        "filename": f"master-{master_asset['id']}.mp4",
                        "kind": "master",
                        "media_type": "video/mp4",
                        "metadata": {
                            "asset_id": master_asset["id"],
                            "engine": rendered_result["engine"],
                            "edl_version": version,
                            **delivery_policy,
                        },
                    }
                ],
            )
        except (DeliveryError, OSError) as exc:
            Path(archive_name).unlink(missing_ok=True)
            raise HTTPException(
                status_code=422,
                detail=f"No se pudo crear el paquete editable: {exc}",
            ) from exc
        return FileResponse(
            archive,
            media_type="application/zip",
            filename="comfyuipod-editable-delivery.zip",
            background=BackgroundTask(lambda: archive.unlink(missing_ok=True)),
        )

    @app.post("/studio/api/projects/{project_id}/reels/render", status_code=201)
    async def render_project_reel(
        project_id: str,
        body: ReelRenderRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Renderiza un reel local con clips, transiciones, títulos y captions."""
        from comfy_studio.engines.editor import Clip, TextOverlay
        from comfy_studio.perception.keyframes import probe_media_info
        from comfy_studio.projects import MediaValidationError
        from comfy_studio.reel_presets import resolve_reel_preset

        active_store: ProjectStore = request.app.state.store
        active_cfg: StudioConfig = request.app.state.config
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        clips: list[Clip] = []
        source_ids: list[str] = []
        for index, request_clip in enumerate(body.clips, start=1):
            asset = active_store.get_asset(project_id, request_clip.asset_id)
            source = (
                active_store.get_asset_path(project_id, request_clip.asset_id) if asset else None
            )
            if asset is None or source is None or asset.get("kind") != "video":
                raise HTTPException(
                    status_code=400,
                    detail=f"El medio del clip {index} no es un vídeo disponible del proyecto",
                )
            info = probe_media_info(
                source,
                timeout_s=active_cfg.studio_ffprobe_timeout_s,
            )
            native_duration = float(info.get("duration_sec") or 0.0)
            if not info.get("probe_ok") or not info.get("has_video") or native_duration <= 0:
                raise HTTPException(
                    status_code=400,
                    detail=f"No se pudo verificar la duración del clip {index}",
                )
            duration = request_clip.duration_s or (native_duration - request_clip.start_s)
            if (
                request_clip.start_s >= native_duration
                or request_clip.start_s + duration > native_duration + 0.05
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"La duración solicitada para el clip {index} excede su duración fuente",
                )
            clips.append(
                Clip(
                    path=source,
                    duration_s=duration,
                    start_s=request_clip.start_s,
                    transition_in=request_clip.transition_in,
                    transition_duration_s=request_clip.transition_duration_s,
                )
            )
            source_ids.append(request_clip.asset_id)

        requested_duration = 0.0
        previous_duration = 0.0
        for index, clip in enumerate(clips):
            overlap = (
                min(clip.transition_duration_s, clip.duration_s, previous_duration)
                if index > 0 and clip.transition_in in {"crossfade", "slide"}
                else 0.0
            )
            requested_duration += clip.duration_s - overlap
            previous_duration = clip.duration_s
        try:
            preset, output_width, output_height, output_fps = resolve_reel_preset(
                body.preset_id,
                width=body.width,
                height=body.height,
                fps=body.fps,
                duration_s=requested_duration,
                clip_count=len(clips),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        for overlay in [*body.titles, *body.captions]:
            if overlay.end_s > requested_duration:
                raise HTTPException(
                    status_code=422,
                    detail="El texto del reel no puede extenderse más allá de la duración final",
                )

        audio_path: Path | None = None
        if body.audio_asset_id:
            audio_asset = active_store.get_asset(project_id, body.audio_asset_id)
            audio_path = (
                active_store.get_asset_path(project_id, body.audio_asset_id)
                if audio_asset
                else None
            )
            if audio_asset is None or audio_path is None or audio_asset.get("kind") != "audio":
                raise HTTPException(status_code=400, detail="La pista de audio no está disponible")

        titles = [TextOverlay(**item.model_dump()) for item in body.titles]
        captions = [TextOverlay(**item.model_dump()) for item in body.captions]
        with tempfile.TemporaryDirectory(prefix="studio-reel-output-") as output_dir:
            output_path = Path(output_dir) / "reel.mp4"
            job = {
                "clips": [
                    {
                        "path": str(clip.path),
                        "start_s": clip.start_s,
                        "duration_s": clip.duration_s,
                        "transition_in": clip.transition_in,
                        "transition_duration_s": clip.transition_duration_s,
                    }
                    for clip in clips
                ],
                "output_path": str(output_path),
                "width": output_width,
                "height": output_height,
                "fps": output_fps,
                "render_concurrency": preset.concurrency,
                "offthread_cache_bytes": preset.offthread_cache_bytes,
                "memory_efficient": True,
                "audio_path": str(audio_path) if audio_path else None,
                "titles": [
                    {
                        "text": item.text,
                        "start_s": item.start_s,
                        "end_s": item.end_s,
                        "position": item.position,
                    }
                    for item in titles
                ],
                "captions": [
                    {
                        "text": item.text,
                        "start_s": item.start_s,
                        "end_s": item.end_s,
                        "position": item.position,
                    }
                    for item in captions
                ],
            }
            result = await run_in_threadpool(
                EditorEngine(backend="remotion").run,
                EngineRunSpec(job=job, timeout_s=active_cfg.studio_ffmpeg_timeout_s),
            )
            rendered_path = Path(str(result.payload.get("output") or ""))
            if not result.ok or result.simulated or not rendered_path.is_file():
                raise HTTPException(
                    status_code=503,
                    detail="Falló el render Remotion; no se publicó una salida simulada",
                )
            output_info = probe_media_info(
                rendered_path,
                timeout_s=active_cfg.studio_ffprobe_timeout_s,
            )
            if (
                not output_info.get("probe_ok")
                or not output_info.get("has_video")
                or int(output_info.get("width") or 0) != output_width
                or int(output_info.get("height") or 0) != output_height
                or abs(float(output_info.get("duration_sec") or 0.0) - requested_duration)
                > (1 / output_fps + 0.05)
            ):
                raise HTTPException(
                    status_code=503,
                    detail="La salida Remotion no pasó la verificación de vídeo",
                )
            if rendered_path.stat().st_size > active_cfg.max_upload_bytes:
                raise HTTPException(status_code=413, detail="El reel renderizado supera el límite")
            try:
                output_asset = active_store.save_upload(
                    project_id,
                    filename="reel.mp4",
                    content_type="video/mp4",
                    data=rendered_path.read_bytes(),
                    max_upload_bytes=active_cfg.max_upload_bytes,
                    category="outputs",
                    metadata={
                        "engine": "remotion",
                        "preset_id": preset.preset_id,
                        "preset_version": 1,
                        "timeline_duration_s": round(requested_duration, 3),
                        "simulated": result.simulated,
                        "source_asset_ids": source_ids,
                        "audio_asset_id": body.audio_asset_id,
                        "titles": [item.text for item in titles],
                        "captions": [item.text for item in captions],
                        "transitions": [clip.transition_in for clip in clips],
                        "width": output_width,
                        "height": output_height,
                        "fps": output_fps,
                    },
                )
            except (LookupError, MediaValidationError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "rendered",
            "project_id": project_id,
            "output_asset": output_asset,
            "engine": "remotion",
            "simulated": result.simulated,
        }

    @app.get("/studio/api/reels/presets")
    async def list_project_reel_presets() -> dict[str, Any]:
        """Lista presets inmutables, versionados y con límites de render."""
        from comfy_studio.reel_presets import list_reel_presets

        return {"presets": list_reel_presets()}

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
