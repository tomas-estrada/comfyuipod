"""Memoria espacial persistente por proyecto (W17 / A19)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from comfy_studio.projects import ProjectStore
from comfy_studio.spatial_memory import Anchor, Provenance, SpatialError, SpatialMemory

_META_KEY = "ultra_spatial_memory_v1"


class AnchorRequest(BaseModel):
    provenance: Provenance = Provenance.OBSERVED


class VisitRequest(BaseModel):
    shot_id: str
    position: tuple[float, float, float]


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> SpatialMemory:
    raw = store.get_meta(project_id, _META_KEY)
    return SpatialMemory.from_dict(raw) if raw is not None else SpatialMemory()


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
