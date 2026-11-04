"""Montaje asistido desde brief: planificación de EDL con evidencia (M7).

Nuevo router aislado; no toca media_ops/perception. El EDL resultante es un
`comfy_studio.edl.v1` estándar (`kind="montage"`), así que se lista, edita y
renderiza con los endpoints de EDL ya existentes.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio.config import StudioConfig
from comfy_studio.edl import execute_edl_ffmpeg
from comfy_studio.montage import MontageBriefRequest, plan_montage_from_brief
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


def _ml_simulated(request: Request) -> bool:
    """True salvo que el backend de percepción esté en modo real verificado."""
    backend = getattr(request.app.state, "perception_backend", "sim")
    probe = getattr(request.app.state, "perception_probe_result", {}) or {}
    return not (backend == "real" and bool(probe.get("probe_ok", False)))


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de montaje sobre `app` (mismo route_class)."""

    @app.post("/studio/api/projects/{project_id}/montage/from-brief")
    async def plan_montage_from_brief_endpoint(
        project_id: str,
        body: MontageBriefRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        active_broker: ProgressBroker = request.app.state.broker
        active_cfg: StudioConfig = request.app.state.config
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        clip_index = getattr(request.app.state, "clip_index", None)
        face_index = getattr(request.app.state, "face_index", None)
        perception_available = bool(getattr(request.app.state, "perception_available", False))
        perception_stale = bool(getattr(request.app.state, "perception_stale", False))

        try:
            plan = await run_in_threadpool(
                plan_montage_from_brief,
                active_store,
                project_id,
                brief=body.brief,
                clip_index=clip_index,
                face_index=face_index,
                perception_available=perception_available,
                perception_stale=perception_stale,
                ml_simulated=_ml_simulated(request),
                title=body.title,
                aspect_ratio=body.aspect_ratio,  # type: ignore[arg-type]
                target_duration_sec=body.target_duration_sec,
                max_cuts=body.max_cuts,
                min_segment_sec=body.min_segment_sec,
                max_segment_sec=body.max_segment_sec,
                asset_ids=body.asset_ids,
                person=body.person,
                time_range_sec=body.time_range_sec,
                ffprobe_timeout_s=active_cfg.studio_ffprobe_timeout_s,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        active_broker.publish(
            "montage",
            {
                "project_id": project_id,
                "edl_id": plan["edl_id"],
                "version": plan["version"],
                "brief": plan["brief"],
                "cuts_count": len(plan["cuts"]),
                "total_duration_sec": plan["total_duration_sec"],
                "evidence_coverage": plan["evidence_coverage"],
                "simulated": plan["simulated"],
                "status": "planned",
            },
        )

        result: dict[str, Any] = {**plan, "render": None}
        if body.render:
            try:
                render_result = await run_in_threadpool(
                    execute_edl_ffmpeg,
                    active_store,
                    project_id,
                    edl_id=plan["edl_id"],
                    aspect_ratio=body.aspect_ratio,
                    fps=body.fps,
                    ffmpeg_timeout_s=active_cfg.studio_ffmpeg_timeout_s,
                    ffprobe_timeout_s=active_cfg.studio_ffprobe_timeout_s,
                )
            except (ValueError, RuntimeError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            active_broker.publish(
                "edl",
                {
                    "project_id": project_id,
                    "edl_id": render_result["edl_id"],
                    "version": render_result["version"],
                    "aspect_ratio": render_result["aspect_ratio"],
                    "output_asset_id": render_result["output_asset"]["id"],
                    "status": "built",
                    "origin": "montage",
                },
            )
            result["render"] = render_result
            result["status"] = "planned_and_rendered"
        return result
