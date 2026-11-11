"""Lexicones de pronunciación y pistas de voz persistidos por proyecto (W19/A21)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from comfy_studio.projects import ProjectStore
from comfy_studio.voice import (
    PronunciationLexicon,
    Utterance,
    VoiceError,
    VoiceTrack,
    assert_approved_variant,
)

_META_KEY = "ultra_voice_assets_v1"


class LexiconRequest(BaseModel):
    entries: dict[str, str] = Field(default_factory=dict)
    approved_variant: bool = False


class UtteranceRequest(BaseModel):
    text: str
    start_s: float
    end_s: float


class VoiceTrackRequest(BaseModel):
    utterances: list[UtteranceRequest] = Field(default_factory=list)


class LexiconValidationRequest(BaseModel):
    sample: list[str] = Field(min_length=1)
    min_coverage: float = Field(default=0.8, ge=0, le=1)


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load(store: ProjectStore, project_id: str) -> dict[str, Any]:
    raw = store.get_meta(project_id, _META_KEY)
    if raw is None:
        return {"schema": "voice-assets/1", "lexicons": {}, "tracks": {}}
    if not isinstance(raw, dict) or raw.get("schema") != "voice-assets/1":
        raise VoiceError("almacenamiento de voz con schema inválido")
    lexicons = raw.get("lexicons")
    tracks = raw.get("tracks")
    if not isinstance(lexicons, dict) or not isinstance(tracks, dict):
        raise VoiceError("almacenamiento de voz incompleto")
    for locale, data in lexicons.items():
        if PronunciationLexicon.from_dict(data).locale != locale:
            raise VoiceError("locale inconsistente en lexicon persistido")
    for speaker, data in tracks.items():
        if VoiceTrack.from_dict(data).speaker != speaker:
            raise VoiceError("speaker inconsistente en pista persistida")
    return {"schema": "voice-assets/1", "lexicons": lexicons, "tracks": tracks}


def _save(store: ProjectStore, project_id: str, data: dict[str, Any]) -> None:
    store.set_meta(project_id, _META_KEY, data)


def register_routes(app: FastAPI) -> None:
    """Persiste lexicones y pistas editables; no afirma que se haya generado audio."""

    @app.get("/studio/api/projects/{project_id}/voice")
    async def get_voice_assets(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            return {**_load(store, project_id), "synthesis": "not_run"}
        except VoiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/studio/api/projects/{project_id}/voice/lexicons/{locale}")
    async def put_voice_lexicon(
        project_id: str,
        locale: str,
        body: LexiconRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            data = _load(store, project_id)
            lexicon = PronunciationLexicon(locale=locale, approved_variant=body.approved_variant)
            for text, phonetic in body.entries.items():
                lexicon.add(text, phonetic)
            data["lexicons"][locale] = lexicon.to_dict()
            _save(store, project_id, data)
            return lexicon.to_dict()
        except VoiceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/voice/lexicons/{locale}")
    async def get_voice_lexicon(
        project_id: str,
        locale: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            data = _load(store, project_id)
            if locale not in data["lexicons"]:
                raise HTTPException(status_code=404, detail="Lexicon no encontrado")
            return data["lexicons"][locale]
        except VoiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/voice/lexicons/{locale}/validate")
    async def validate_voice_lexicon(
        project_id: str,
        locale: str,
        body: LexiconValidationRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            data = _load(store, project_id)
            if locale not in data["lexicons"]:
                raise HTTPException(status_code=404, detail="Lexicon no encontrado")
            lexicon = PronunciationLexicon.from_dict(data["lexicons"][locale])
            coverage = lexicon.coverage(body.sample)
            try:
                assert_approved_variant(lexicon, body.sample, min_cov=body.min_coverage)
            except VoiceError as exc:
                return {
                    "locale": locale,
                    "approved": False,
                    "coverage": coverage,
                    "required_coverage": body.min_coverage,
                    "reason": str(exc),
                }
            return {
                "locale": locale,
                "approved": True,
                "coverage": coverage,
                "required_coverage": body.min_coverage,
                "reason": None,
            }
        except VoiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/studio/api/projects/{project_id}/voice/tracks/{speaker}")
    async def put_voice_track(
        project_id: str,
        speaker: str,
        body: VoiceTrackRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            data = _load(store, project_id)
            track = VoiceTrack(
                speaker=speaker,
                utterances=[
                    Utterance(
                        speaker=speaker,
                        text=item.text,
                        start_s=item.start_s,
                        end_s=item.end_s,
                    )
                    for item in body.utterances
                ],
            )
            data["tracks"][speaker] = track.to_dict()
            _save(store, project_id, data)
            return track.to_dict()
        except VoiceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/voice/tracks/{speaker}")
    async def get_voice_track(
        project_id: str,
        speaker: str,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            data = _load(store, project_id)
            if speaker not in data["tracks"]:
                raise HTTPException(status_code=404, detail="Pista de voz no encontrada")
            return data["tracks"][speaker]
        except VoiceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
