"""Persistencia del mix causal y sus stems por proyecto (W18/A20)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from comfy_studio.audio import AudioError, Mix, SoundEvent, Stem, delete_cause
from comfy_studio.projects import ProjectStore

_META_KEY = "ultra_audio_mix_v1"


class SoundEventRequest(BaseModel):
    event_id: str
    onset_s: float
    loudness_lufs: float = -23.0
    caused_by: str | None = None


class MixRequest(BaseModel):
    fps: int = Field(ge=1)
    duration_s: float = Field(default=0.0, ge=0)
    stems: dict[str, list[SoundEventRequest]] = Field(default_factory=dict)


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> Mix | None:
    raw = store.get_meta(project_id, _META_KEY)
    return Mix.from_dict(raw) if raw is not None else None


def _from_request(body: MixRequest) -> Mix:
    mix = Mix(
        fps=body.fps,
        duration_s=body.duration_s,
        stems=[
            Stem(
                name=name,
                events=[
                    SoundEvent(
                        event_id=event.event_id,
                        onset_s=event.onset_s,
                        loudness_lufs=event.loudness_lufs,
                        caused_by=event.caused_by,
                    )
                    for event in events
                ],
            )
            for name, events in body.stems.items()
        ],
    )
    mix.validate()
    return mix


def _response(mix: Mix) -> dict[str, Any]:
    return {
        **mix.to_dict(),
        "event_count": len(mix.all_events()),
        "audio_generation": "not_run",
        "separation_claim": "metadata_only",
    }


def register_routes(app: FastAPI) -> None:
    """Guarda eventos y stems editables; no afirma que haya generado audio."""

    @app.get("/studio/api/projects/{project_id}/sound/mix")
    async def get_project_mix(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            mix = _load(store, project_id)
        except AudioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if mix is None:
            raise HTTPException(status_code=404, detail="Mix no encontrado")
        return _response(mix)

    @app.put("/studio/api/projects/{project_id}/sound/mix")
    async def put_project_mix(
        project_id: str,
        body: MixRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            mix = _from_request(body)
            store.set_meta(project_id, _META_KEY, mix.to_dict())
            return _response(mix)
        except AudioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.delete("/studio/api/projects/{project_id}/sound/causes/{cause_id}")
    async def delete_sound_cause(
        project_id: str,
        cause_id: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            mix = _load(store, project_id)
        except AudioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if mix is None:
            raise HTTPException(status_code=404, detail="Mix no encontrado")
        removed = sorted(delete_cause(mix, cause_id))
        store.set_meta(project_id, _META_KEY, mix.to_dict())
        return {**_response(mix), "removed_event_ids": removed}
