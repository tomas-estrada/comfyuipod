"""Percepción: keyframes, índices CLIP/faces, búsquedas e identidades.

Extraído de app.py (M7-SCR-002).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from comfy_studio import SIMULATED_ML_NOTICE
from comfy_studio.perception import (
    CLIP_EMBED_DIM,
    FACE_EMBED_DIM,
    INSIGHTFACE_NC_LICENSE_NOTICE,
    ClipVisualIndex,
    InsightFaceIndex,
    PerceptionVectorStore,
    extract_video_keyframes,
)
from comfy_studio.projects import ProjectStore
from comfy_studio.webutil import (
    FaceIndexRequest,
    IdentityCreateRequest,
    IdentityRenameRequest,
    KeyframeExtractRequest,
    PerceptionIndexRequest,
    PerceptionSearchRequest,
    _require_perception,
)


def _simulation_fields(request: Request) -> dict[str, Any]:
    """Estado real/simulado del backend que sirvió la búsqueda.

    Las respuestas de búsqueda fijaban `simulated: True`: en modo `real` un
    índice real se anunciaba como simulado y arrastraba un aviso que no
    aplicaba. La fuente es `app.state.perception_backend`, que `create_app`
    resuelve desde `STUDIO_PERCEPTION_BACKEND` y el probe de arranque.
    """
    if getattr(request.app.state, "perception_backend", "sim") == "sim":
        return {"simulated": True, "simulation_notice": SIMULATED_ML_NOTICE}
    return {"simulated": False}


def register_routes(app: FastAPI) -> None:
    """Registra las rutas de este módulo directamente sobre `app` (mismo route_class)."""
    @app.get("/studio/api/projects/{project_id}/keyframes")
    async def list_project_keyframes(
        project_id: str,
        request: Request,
        source_asset_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        kf_assets = active_store.list_assets(project_id, category="keyframes")
        items: list[dict[str, Any]] = []
        for asset in kf_assets:
            meta = dict(asset.get("metadata") or {})
            src_aid = meta.get("source_asset_id") or asset["id"]
            if source_asset_id and src_aid != source_asset_id:
                continue
            items.append(
                {
                    **asset,
                    "source_asset_id": src_aid,
                    "timestamp_sec": float(meta.get("timestamp_sec") or 0.0),
                    "frame_index": int(meta.get("frame_index") or 0),
                }
            )
        items.sort(
            key=lambda x: (str(x.get("source_asset_id")), float(x.get("timestamp_sec") or 0.0))
        )
        return {
            "project_id": project_id,
            "count": len(items),
            "keyframes": items,
        }
    @app.post("/studio/api/projects/{project_id}/keyframes/extract")
    async def extract_project_keyframes(
        project_id: str,
        body: KeyframeExtractRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        target_aid = body.asset_id
        if not target_aid:
            videos = active_store.list_assets(project_id, kind="video")
            if not videos:
                raise HTTPException(
                    status_code=400,
                    detail="El proyecto no contiene ningún vídeo para extraer keyframes.",
                )
            target_aid = videos[0]["id"]
        try:
            kfs = await run_in_threadpool(
                extract_video_keyframes,
                active_store,
                project_id,
                target_aid,
                interval_sec=body.interval_sec,
                scene_threshold=body.scene_threshold,
                max_frames=body.max_frames,
                ffmpeg_timeout_s=request.app.state.config.studio_ffmpeg_timeout_s,
                ffprobe_timeout_s=request.app.state.config.studio_ffprobe_timeout_s,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "status": "extracted",
            "project_id": project_id,
            "source_asset_id": target_aid,
            "count": len(kfs),
            "keyframes": kfs,
        }
    @app.post("/studio/api/projects/{project_id}/perception/index")
    async def index_project_perception(
        project_id: str,
        body: PerceptionIndexRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        # M3.3: índice stale ⇒ escrituras rechazan con 409 hasta reindexar.
        if request.app.state.perception_stale:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Índice de percepción desactualizado (dims almacenadas no coinciden "
                    "con las activas). Ejecuta POST /studio/api/perception/reindex antes "
                    "de escribir nuevos embeddings."
                ),
            )

        # Trabajo síncrono pesado (ffmpeg/embeddings/sqlite): fuera del event loop para
        # no bloquear /health, el SSE de progreso ni el resto de peticiones.
        def _index_sync() -> dict[str, Any]:
            if body.asset_id:
                res = active_clip.index_asset(
                    project_id,
                    body.asset_id,
                    interval_sec=body.interval_sec,
                )
                faces_count = 0
                if body.detect_faces:
                    faces_count += len(
                        active_face.detect_and_index_asset(project_id, body.asset_id)
                    )
                    for shot in res.get("shots", []):
                        kf_id = shot.get("keyframe_asset_id")
                        if kf_id and kf_id != body.asset_id:
                            faces_count += len(
                                active_face.detect_and_index_asset(
                                    project_id,
                                    kf_id,
                                    source_asset_id=body.asset_id,
                                    timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                                )
                            )
                return {
                    **res,
                    "faces_detected": faces_count,
                    "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
                }
            res = active_clip.index_project_media(
                project_id,
                interval_sec=body.interval_sec,
            )
            faces_count = 0
            if body.detect_faces:
                for item in res.get("results", []):
                    src_aid = item["asset_id"]
                    for shot in item.get("shots", []):
                        kf_id = shot.get("keyframe_asset_id") or src_aid
                        faces_count += len(
                            active_face.detect_and_index_asset(
                                project_id,
                                kf_id,
                                source_asset_id=src_aid,
                                timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                            )
                        )
            return {
                **res,
                "faces_detected": faces_count,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }

        try:
            return await run_in_threadpool(_index_sync)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @app.post("/studio/api/projects/{project_id}/faces/index")
    async def index_project_faces_endpoint(
        project_id: str,
        request: Request,
        body: FaceIndexRequest | None = None,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        req_body = body or FaceIndexRequest()
        try:
            registered_identity = None
            if req_body.name:
                registered_identity = active_face.register_identity(
                    project_id,
                    name=req_body.name,
                    reference_asset_id=req_body.reference_asset_id,
                )
            res = await run_in_threadpool(
                active_face.index_project_faces,
                project_id,
                keyframe_asset_ids=req_body.keyframe_asset_ids,
            )
            return {
                **res,
                "identity": registered_identity,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @app.get("/studio/api/projects/{project_id}/perception/search")
    async def search_project_perception_get(
        project_id: str,
        request: Request,
        query: str | None = Query(default=None),
        asset_id: str | None = Query(default=None),
        identity_id: str | None = Query(default=None),
        identity_name: str | None = Query(default=None),
        reference_asset_id: str | None = Query(default=None),
        limit: int = Query(default=10, ge=1, le=100),
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")

        # M3.3: índice stale ⇒ lecturas devuelven vacío con stale=true, nunca ranking erróneo.
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "text",
                "query": query or "",
                "count": 0,
                "shots": [],
                "stale": True,
                **_simulation_fields(request),
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }

        if identity_id or identity_name or reference_asset_id:
            try:
                shots = active_face.find_shots_face(
                    project_id,
                    identity_id=identity_id,
                    identity_name=identity_name,
                    reference_asset_id=reference_asset_id,
                    text_query=query,
                    limit=limit,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return {
                "project_id": project_id,
                "mode": "combined" if query else "face",
                "query": query,
                "identity_id": identity_id,
                "identity_name": identity_name,
                "count": len(shots),
                "shots": shots,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
                **_simulation_fields(request),
            }

        shots = active_clip.find_shots_text(
            project_id,
            query or "",
            limit=limit,
            asset_id=asset_id,
        )
        return {
            "project_id": project_id,
            "mode": "text",
            "query": query or "",
            "count": len(shots),
            "shots": shots,
            **_simulation_fields(request),
        }
    @app.post("/studio/api/projects/{project_id}/perception/search")
    async def search_project_perception_post(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        eff_query = body.query if body.query is not None else body.text_query
        eff_name = body.identity_name or body.person_name
        return await search_project_perception_get(
            project_id=project_id,
            request=request,
            query=eff_query,
            asset_id=body.asset_id,
            identity_id=body.identity_id,
            identity_name=eff_name,
            reference_asset_id=body.reference_asset_id,
            limit=body.limit,
        )
    @app.post("/studio/api/projects/{project_id}/search/faces")
    async def search_project_faces_endpoint(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "face",
                "identity_id": body.identity_id,
                "identity_name": body.identity_name or body.person_name,
                "count": 0,
                "shots": [],
                "stale": True,
                **_simulation_fields(request),
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }
        eff_name = body.identity_name or body.person_name
        try:
            shots = active_face.find_shots_face(
                project_id,
                identity_id=body.identity_id,
                identity_name=eff_name,
                reference_asset_id=body.reference_asset_id,
                limit=body.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "mode": "face",
            "identity_id": body.identity_id,
            "identity_name": eff_name,
            "count": len(shots),
            "shots": shots,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            **_simulation_fields(request),
        }
    @app.post("/studio/api/projects/{project_id}/search/combined")
    async def search_project_combined_endpoint(
        project_id: str,
        body: PerceptionSearchRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        if request.app.state.perception_stale:
            return {
                "project_id": project_id,
                "mode": "combined",
                "query": body.query if body.query is not None else body.text_query,
                "identity_id": body.identity_id,
                "identity_name": body.identity_name or body.person_name,
                "count": 0,
                "shots": [],
                "stale": True,
                **_simulation_fields(request),
                "message": (
                    "Índice de percepción desactualizado (dims no coinciden). "
                    "Ejecuta POST /studio/api/perception/reindex."
                ),
            }
        eff_query = body.query if body.query is not None else body.text_query
        eff_name = body.identity_name or body.person_name
        try:
            shots = active_face.combined_search(
                project_id,
                identity_id=body.identity_id,
                identity_name=eff_name,
                reference_asset_id=body.reference_asset_id,
                text_query=eff_query,
                limit=body.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "project_id": project_id,
            "mode": "combined",
            "query": eff_query,
            "identity_id": body.identity_id,
            "identity_name": eff_name,
            "count": len(shots),
            "shots": shots,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            **_simulation_fields(request),
        }
    @app.post("/studio/api/perception/reindex")
    async def perception_reindex(request: Request) -> dict[str, Any]:
        """Reconstruye los índices vectoriales con las dims activas (M3.3).

        - Elimina y recrea `vec_clip_shots` / `vec_face_identities` / `vec_face_detections`
          con las dims del backend activo (768/512).
        - Re-embede desde los keyframes PNG existentes en `clip_shots` /
          `face_detections` (los PNG y `studio.db` no se tocan).
        - Reescribe `schema_meta` con `clip_dim`, `face_dim`, extractores y `built_at`.
        - Solo se permite cuando el índice está `stale`; si no hay desajuste, devuelve
          `already_current` sin hacer nada.
        """
        _require_perception(request)
        active_store: ProjectStore = request.app.state.store
        active_clip: ClipVisualIndex = request.app.state.clip_index
        active_face: InsightFaceIndex = request.app.state.face_index
        vs: PerceptionVectorStore = request.app.state.vector_store
        if vs is None or active_clip is None or active_face is None:
            raise HTTPException(
                status_code=503,
                detail="Percepción vectorial no disponible para reindexar.",
            )

        stored_clip, stored_face = vs.stored_dims()
        if (
            stored_clip == CLIP_EMBED_DIM
            and stored_face == FACE_EMBED_DIM
            and stored_clip is not None
        ):
            return {
                "status": "already_current",
                "clip_dim": CLIP_EMBED_DIM,
                "face_dim": FACE_EMBED_DIM,
                "message": "El índice ya usa las dims activas; no es necesario reindexar.",
            }

        # Ejecutar reindex síncrono fuera del event loop (pesado: sqlite + embeddings).
        def _reindex_sync() -> dict[str, Any]:
            with vs._connection() as conn:
                # 1. Dropear vec tables
                conn.execute("DROP TABLE IF EXISTS vec_clip_shots")
                conn.execute("DROP TABLE IF EXISTS vec_face_identities")
                conn.execute("DROP TABLE IF EXISTS vec_face_detections")
                # 2. Recrear con dims activas
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_clip_shots "
                    f"USING vec0(embedding float[{CLIP_EMBED_DIM}])"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_face_identities "
                    f"USING vec0(embedding float[{FACE_EMBED_DIM}])"
                )
                conn.execute(
                    "CREATE VIRTUAL TABLE vec_face_detections "
                    f"USING vec0(embedding float[{FACE_EMBED_DIM}])"
                )
                # 3. Borrar shots/identities/detecciones existentes (se reconstruyen)
                conn.execute("DELETE FROM clip_shots")
                conn.execute("DELETE FROM face_identities")
                conn.execute("DELETE FROM face_detections")
                # 4. Reescribir schema_meta
                from datetime import UTC, datetime
                now = datetime.now(UTC).isoformat()
                vs.set_schema_meta("clip_dim", str(CLIP_EMBED_DIM))
                vs.set_schema_meta("face_dim", str(FACE_EMBED_DIM))
                vs.set_schema_meta("clip_extractor", "openai/clip-vit-large-patch14")
                vs.set_schema_meta("face_extractor", "buffalo_l/w600k_r50")
                vs.set_schema_meta("built_at", now)
                conn.commit()

            # 5. Re-indexar todos los keyframes existentes del proyecto (por defecto el primero)
            projects = active_store.list_projects()
            if not projects:
                return {
                    "status": "done",
                    "clip_dim": CLIP_EMBED_DIM,
                    "face_dim": FACE_EMBED_DIM,
                    "projects_reindexed": 0,
                    "message": "No hay proyectos; índice recreado vacío.",
                }
            target_project = projects[0]["id"]
            res = active_clip.index_project_media(target_project)
            return {
                "status": "done",
                "clip_dim": CLIP_EMBED_DIM,
                "face_dim": FACE_EMBED_DIM,
                "project_id": target_project,
                "shots_indexed": res.get("indexed_shots_count", 0),
                "skipped_synthetic": res.get("skipped_synthetic_count", 0),
                "message": "Índice reconstruido con dims activas.",
            }


        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _reindex_sync)
        # Marcar como no stale tras reindex exitoso
        request.app.state.perception_stale = False
        return result
    @app.get("/studio/api/projects/{project_id}/identities")
    async def list_project_identities(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        items = active_store.list_identities(project_id)
        return {
            "project_id": project_id,
            "count": len(items),
            "identities": items,
            "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
        }
    @app.post("/studio/api/projects/{project_id}/identities", status_code=201)
    async def register_project_identity(
        project_id: str,
        body: IdentityCreateRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            res = active_face.register_identity(
                project_id,
                name=body.name,
                reference_asset_id=body.reference_asset_id,
            )
            return {
                **res,
                "license_notice": INSIGHTFACE_NC_LICENSE_NOTICE,
            }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @app.patch("/studio/api/projects/{project_id}/identities/{identity_id}")
    async def rename_project_identity(
        project_id: str,
        identity_id: str,
        body: IdentityRenameRequest,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        _require_perception(request)
        active_face: InsightFaceIndex = request.app.state.face_index
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        try:
            updated = active_face.rename_identity(project_id, identity_id, body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail="Identidad no encontrada")
        return updated
    @app.delete("/studio/api/projects/{project_id}/identities/{identity_id}")
    async def delete_project_identity(
        project_id: str,
        identity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        active_store: ProjectStore = request.app.state.store
        if active_store.get_project(project_id) is None:
            raise HTTPException(status_code=404, detail="Proyecto no encontrado")
        deleted = active_store.delete_identity(project_id, identity_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Identidad no encontrada")
        return {"deleted": True, "id": identity_id}
