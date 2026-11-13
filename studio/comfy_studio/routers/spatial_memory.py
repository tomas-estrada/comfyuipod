"""Memoria espacial persistente por proyecto (W17 / A19)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from comfy_studio.projects import ProjectStore
from comfy_studio.spatial_memory import (
    Anchor,
    Provenance,
    SpatialError,
    SpatialMemory,
    TemporalScene,
)

_META_KEY = "ultra_spatial_memory_v1"
_SCENE_META_KEY = "ultra_temporal_scene_v1"


class AnchorRequest(BaseModel):
    provenance: Provenance = Provenance.OBSERVED


class VisitRequest(BaseModel):
    shot_id: str
    position: tuple[float, float, float]


class ReframeRequest(BaseModel):
    camera_id: str
    offset: tuple[float, float, float]
    rotate_deg: tuple[float, float, float] = (0.0, 0.0, 0.0)


class RetimeRequest(BaseModel):
    scale: float
    new_fps: int | None = None


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> SpatialMemory:
    raw = store.get_meta(project_id, _META_KEY)
    return SpatialMemory.from_dict(raw) if raw is not None else SpatialMemory()


def _scene_not_found(exc: SpatialError) -> bool:
    text = str(exc)
    return "no registrada" in text or "no registrado" in text


def _load_scene(store: ProjectStore, project_id: str) -> TemporalScene:
    raw = store.get_meta(project_id, _SCENE_META_KEY)
    if raw is None:
        raise SpatialError("escena temporal no registrada para el proyecto")
    return TemporalScene.from_dict(raw)


def _merge_scene(existing: TemporalScene, incoming: TemporalScene) -> TemporalScene:
    """Fusión idempotente: reintentos no-op, escrituras conflictivas fallan."""
    if existing.timebase.fps != incoming.timebase.fps:
        raise SpatialError("conflicto: fps de la escena no puede cambiar al fusionar")
    existing.timebase = incoming.timebase
    existing.duration_s = max(existing.duration_s, incoming.duration_s)
    existing.max_entities = max(existing.max_entities, incoming.max_entities)
    existing.max_keys_per_entity = max(
        existing.max_keys_per_entity, incoming.max_keys_per_entity
    )
    for source_id, ref in incoming.sources.items():
        existing.add_source(source_id, ref)
    for entity in incoming.objects.values():
        for key in entity.keys:
            existing.set_object_key(
                entity.entity_id,
                key.t,
                key.position,
                provenance=entity.provenance,
                source_id=entity.source_id,
            )
    for entity in incoming.poses.values():
        for key in entity.keys:
            existing.set_pose_key(
                entity.entity_id,
                key.t,
                key.angles,
                provenance=entity.provenance,
                source_id=entity.source_id,
            )
    for entity in incoming.cameras.values():
        for key in entity.keys:
            existing.set_camera_key(
                entity.entity_id,
                key.t,
                key.position,
                key.orientation_deg,
                key.fov_deg,
                provenance=entity.provenance,
                source_id=entity.source_id,
            )
    existing.validate()
    return existing


def register_routes(app: FastAPI) -> None:
    """Expone memoria espacial reanudable y su procedencia en el API de Studio."""

    @app.get("/studio/api/projects/{project_id}/spatial-memory")
    async def get_spatial_memory(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            memory = _load(store, project_id)
            return {**memory.to_dict(), "gpu_validation": "not_run"}
        except SpatialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/studio/api/projects/{project_id}/spatial-memory/anchors/{anchor_id}")
    async def register_anchor(
        project_id: str,
        anchor_id: str,
        body: AnchorRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            memory = _load(store, project_id)
            memory.register(Anchor(anchor_id=anchor_id, provenance=body.provenance))
            store.set_meta(project_id, _META_KEY, memory.to_dict())
            return memory.to_dict()["anchors"][anchor_id]
        except (KeyError, SpatialError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/spatial-memory/anchors/{anchor_id}/visits")
    async def add_anchor_visit(
        project_id: str,
        anchor_id: str,
        body: VisitRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            memory = _load(store, project_id)
            memory.visit(anchor_id, body.shot_id, body.position)
            store.set_meta(project_id, _META_KEY, memory.to_dict())
            return memory.to_dict()
        except SpatialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/spatial-memory/anchors/{anchor_id}/round-trip"
    )
    async def get_anchor_round_trip(
        project_id: str,
        anchor_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            return _load(store, project_id).round_trip_drift(anchor_id)
        except SpatialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # --- escena temporal editable (Task 1, C06/C17 / A19) -------------------
    # Representación keyframe editable acotada; NO es reconstrucción 4D
    # neuronal (eso queda explícitamente pendiente y sin reclamar).

    def _scene_payload(scene: TemporalScene) -> dict[str, Any]:
        return {
            **scene.to_dict(),
            "neural_4d_reconstruction": "not_claimed",
            "gpu_validation": "not_run",
        }

    @app.get("/studio/api/projects/{project_id}/temporal-scene")
    async def get_temporal_scene(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            scene = _load_scene(store, project_id)
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return _scene_payload(scene)

    @app.put("/studio/api/projects/{project_id}/temporal-scene")
    async def put_temporal_scene(
        project_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="JSON malformado") from exc
        try:
            incoming = TemporalScene.from_dict(body)
            try:
                raw = store.get_meta(project_id, _SCENE_META_KEY)
                scene = (
                    _merge_scene(TemporalScene.from_dict(raw), incoming)
                    if raw is not None
                    else incoming
                )
            except SpatialError:
                raise
            store.set_meta(project_id, _SCENE_META_KEY, scene.to_dict())
            return _scene_payload(scene)
        except SpatialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/temporal-scene/reframe")
    async def reframe_temporal_scene(
        project_id: str,
        body: ReframeRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            scene = _load_scene(store, project_id)
            summary = scene.reframe(body.camera_id, body.offset, body.rotate_deg)
            store.set_meta(project_id, _SCENE_META_KEY, scene.to_dict())
            return summary
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/temporal-scene/retime")
    async def retime_temporal_scene(
        project_id: str,
        body: RetimeRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            scene = _load_scene(store, project_id)
            summary = scene.retime(body.scale, new_fps=body.new_fps)
            store.set_meta(project_id, _SCENE_META_KEY, scene.to_dict())
            return summary
        except SpatialError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/objects/{entity_id}/round-trip"
    )
    async def get_object_round_trip(
        project_id: str,
        entity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            return _load_scene(store, project_id).object_round_trip(entity_id)
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/poses/{entity_id}/round-trip"
    )
    async def get_pose_round_trip(
        project_id: str,
        entity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            return _load_scene(store, project_id).pose_round_trip(entity_id)
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/cameras/{entity_id}/round-trip"
    )
    async def get_camera_round_trip(
        project_id: str,
        entity_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            return _load_scene(store, project_id).camera_round_trip(entity_id)
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/objects/{entity_id}/sample"
    )
    async def get_object_sample(
        project_id: str,
        entity_id: str,
        request: Request,
        time: float = 0.0,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            scene = _load_scene(store, project_id)
            position = scene.sample_object(entity_id, time)
            return {
                "entity_id": entity_id,
                "time": time,
                "position": list(position),
            }
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/poses/{entity_id}/sample"
    )
    async def get_pose_sample(
        project_id: str,
        entity_id: str,
        request: Request,
        time: float = 0.0,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            angles = _load_scene(store, project_id).sample_pose(entity_id, time)
            return {"entity_id": entity_id, "time": time, "angles": angles}
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get(
        "/studio/api/projects/{project_id}/temporal-scene/cameras/{entity_id}/sample"
    )
    async def get_camera_sample(
        project_id: str,
        entity_id: str,
        request: Request,
        time: float = 0.0,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            camera = _load_scene(store, project_id).sample_camera(entity_id, time)
            return {
                "entity_id": entity_id,
                "time": time,
                "position": list(camera.position),
                "orientation_deg": list(camera.orientation_deg),
                "fov_deg": camera.fov_deg,
            }
        except SpatialError as exc:
            status = 404 if _scene_not_found(exc) else 409
            raise HTTPException(status_code=status, detail=str(exc)) from exc
