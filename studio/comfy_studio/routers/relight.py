"""Persistencia de AOV por proyecto e invalidación selectiva (W16/A18)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from comfy_studio.projects import ProjectStore
from comfy_studio.relight import (
    AOV,
    AOVSet,
    AOVValidity,
    RelightError,
    TaskKind,
    invalidate_for_task,
)

_META_KEY = "ultra_aov_set_v1"


class AOVRequest(BaseModel):
    validity: AOVValidity
    content_hash: str | None = None


class AOVSetRequest(BaseModel):
    aovs: dict[str, AOVRequest] = Field(default_factory=dict)


class InvalidationRequest(BaseModel):
    task: TaskKind


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> AOVSet | None:
    raw = store.get_meta(project_id, _META_KEY)
    return AOVSet.from_dict(raw) if raw is not None else None


def _response(aovset: AOVSet) -> dict[str, Any]:
    return {**aovset.to_dict(), "render_validation": "not_run"}


def register_routes(app: FastAPI) -> None:
    """Guarda estado de pases y persiste invalidación antes de recomputación."""

    @app.get("/studio/api/projects/{project_id}/aovs")
    async def get_project_aovs(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = _load(store, project_id)
        except RelightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if aovset is None:
            raise HTTPException(status_code=404, detail="AOVSet no encontrado")
        return _response(aovset)

    @app.put("/studio/api/projects/{project_id}/aovs")
    async def put_project_aovs(
        project_id: str,
        body: AOVSetRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = AOVSet(
                aovs={
                    name: AOV(
                        name=name,
                        validity=item.validity,
                        content_hash=item.content_hash,
                    )
                    for name, item in body.aovs.items()
                }
            )
            aovset.validate()
            store.set_meta(project_id, _META_KEY, aovset.to_dict())
            return _response(aovset)
        except RelightError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/aovs/invalidate")
    async def invalidate_project_aovs(
        project_id: str,
        body: InvalidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            aovset = _load(store, project_id)
        except RelightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if aovset is None:
            raise HTTPException(status_code=404, detail="AOVSet no encontrado")
        invalidated = sorted(invalidate_for_task(aovset, body.task))
        for name in invalidated:
            aovset.aovs[name].validity = AOVValidity.UNKNOWN
            aovset.aovs[name].content_hash = None
        store.set_meta(project_id, _META_KEY, aovset.to_dict())
        return {**_response(aovset), "task": body.task, "invalidated": invalidated}
