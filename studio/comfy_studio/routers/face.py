"""Curvas de morph facial por hablante persistidas por proyecto (W14/A16).

Expone el contrato determinista de `comfy_studio.face` en el API de Studio:
curva de morph ligada a una pista de audio por `audio_track_id`, guardado
validado (falla cerrada), reintentos idempotentes con conflicto 409, propiedad
por proyecto y export/import reejecutable con SHA-256 canónico (consistente
con routers/motion.py).

A16: la procedencia declara explícitamente que no hay generación neural; la
síntesis facial real es engine GPU y esto es evidencia CPU simbólica.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from comfy_studio.face import (
    FaceError,
    SpeakerMorphCurve,
    compile_spanish_curve,
    render_morph_glb,
    speaker_tracks,
)
from comfy_studio.projects import ProjectStore

_META_KEY = "ultra_face_curves_v1"
_EXPORT_SCHEMA = "ultra-face-curve-export-v1"

#: Declaración honesta (A16): la curva es simbólica, declarada por claves; sin
#: generación neural y sin validación GPU.
_SAVED_CURVE_PROVENANCE: dict[str, Any] = {
    "origin": "declared_keyframes",
    "neural_generation": "not_used",
    "gpu_validation": "not_run",
}


class CurveRequest(BaseModel):
    speaker_id: str
    audio_track_id: str
    times: list[float]
    weights: list[dict[str, float]] = Field(default_factory=list)


class SpeakerTurnsRequest(BaseModel):
    turns: list[dict[str, Any]]


class CompileSpanishCurveRequest(BaseModel):
    audio_track_id: str
    turn_index: int = 0
    rig_mapping: dict[str, str]


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load_doc(store: ProjectStore, project_id: str) -> dict[str, Any]:
    raw = store.get_meta(project_id, _META_KEY)
    doc = raw if isinstance(raw, dict) else {}
    return {"curves": doc.get("curves", {}), "speaker_tracks": doc.get("speaker_tracks", {})}


def _save_doc(store: ProjectStore, project_id: str, doc: dict[str, Any]) -> None:
    store.set_meta(project_id, _META_KEY, doc)


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _curve_hash(curve_dict: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(curve_dict).encode("utf-8")).hexdigest()


def _record(curve_dict: dict[str, Any], provenance: dict[str, Any]) -> dict[str, Any]:
    return {
        "curve": curve_dict,
        "provenance": dict(provenance),
        "content_hash": _curve_hash(curve_dict),
    }


def _curve_from_request(curve_id: str, body: CurveRequest) -> SpeakerMorphCurve:
    curve = SpeakerMorphCurve(
        curve_id=curve_id,
        speaker_id=body.speaker_id,
        audio_track_id=body.audio_track_id,
        times=[float(t) for t in body.times],
        weights=[
            {str(target): float(weight) for target, weight in w.items()} for w in body.weights
        ],
    )
    curve.validate()
    return curve


def register_routes(app: FastAPI) -> None:
    """Expone curvas faciales persistidas por proyecto en /studio/api."""

    @app.put("/studio/api/projects/{project_id}/face/curves/{curve_id}")
    async def put_curve(
        project_id: str, curve_id: str, body: CurveRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            curve = _curve_from_request(curve_id, body)
            curve_dict = curve.to_dict()
            record = _record(curve_dict, _SAVED_CURVE_PROVENANCE)
            doc = _load_doc(store, project_id)
            existing = doc["curves"].get(curve_id)
            if existing is not None and existing != record:
                raise FaceError(f"curva {curve_id!r} ya existe con contenido distinto; usa otro id")
            if existing is None:
                doc["curves"][curve_id] = record
                _save_doc(store, project_id, doc)
            return {
                **curve_dict,
                "provenance": record["provenance"],
                "content_hash": record["content_hash"],
            }
        except (KeyError, TypeError, ValueError, FaceError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/face/curves")
    async def list_curves(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        return {"curves": _load_doc(store, project_id)["curves"]}

    @app.get("/studio/api/projects/{project_id}/face/curves/{curve_id}")
    async def get_curve(project_id: str, curve_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        record = _load_doc(store, project_id)["curves"].get(curve_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Curva no encontrada: {curve_id}")
        return record

    @app.get("/studio/api/projects/{project_id}/face/curves/{curve_id}/export")
    async def export_curve(project_id: str, curve_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        record = _load_doc(store, project_id)["curves"].get(curve_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Curva no encontrada: {curve_id}")
        curve_dict = record["curve"]
        return {
            "schema": _EXPORT_SCHEMA,
            "origin": {
                "project_id": project_id,
                "curve_id": curve_id,
                "speaker_id": curve_dict["speaker_id"],
                "audio_track_id": curve_dict["audio_track_id"],
            },
            "curve": curve_dict,
            "provenance": record["provenance"],
            "content_hash": record["content_hash"],
        }

    @app.post("/studio/api/projects/{project_id}/face/import")
    async def import_curve(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise FaceError("documento de importación: se esperaba un objeto JSON")
            if body.get("schema") != _EXPORT_SCHEMA:
                raise FaceError("documento de importación: schema desconocido")
            curve_dict = body.get("curve")
            origin = body.get("origin")
            if not isinstance(curve_dict, dict) or not isinstance(origin, dict):
                raise FaceError("documento de importación requiere curve y origin")
            curve = SpeakerMorphCurve.from_dict(curve_dict)
            normalized = curve.to_dict()
            expected_hash = _curve_hash(normalized)
            if body.get("content_hash") != expected_hash:
                raise FaceError(
                    "documento de importación: content_hash no coincide "
                    "(documento alterado o no canónico)"
                )
            curve_id = normalized["curve_id"]
            record = _record(
                normalized,
                {
                    **dict(body.get("provenance", {})),
                    "source_project_id": origin.get("project_id"),
                    "imported_from_schema": _EXPORT_SCHEMA,
                },
            )
            doc = _load_doc(store, project_id)
            existing = doc["curves"].get(curve_id)
            if existing is not None and existing != record:
                raise FaceError(f"curva {curve_id!r} ya existe con contenido distinto")
            doc["curves"][curve_id] = record
            _save_doc(store, project_id, doc)
            return record
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=409,
                detail="documento de importación: JSON malformado",
            ) from exc
        except (KeyError, TypeError, ValueError, FaceError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.put("/studio/api/projects/{project_id}/face/speaker-tracks")
    async def put_speaker_tracks(
        project_id: str, body: SpeakerTurnsRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        prepared = []
        for turn in body.turns:
            asset_id = str(turn.get("audio_asset_id") or "")
            asset = store.get_asset(project_id, asset_id)
            path = store.get_asset_path(project_id, asset_id)
            if not asset or asset.get("kind") != "audio" or path is None:
                raise HTTPException(
                    status_code=422, detail=f"Audio de proyecto no válido: {asset_id}"
                )
            prepared.append({**turn, "audio_sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        try:
            tracks = speaker_tracks(prepared)
        except FaceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        doc = _load_doc(store, project_id)
        doc["speaker_tracks"] = tracks
        _save_doc(store, project_id, doc)
        return {
            "project_id": project_id,
            "tracks": tracks,
            "provenance": {"audio_owner": "project_assets", "identity_measurement": "fixture_only"},
        }

    @app.post(
        "/studio/api/projects/{project_id}/face/speakers/{speaker_id}/curves/{curve_id}/compile-spanish"
    )
    async def compile_curve(
        project_id: str,
        speaker_id: str,
        curve_id: str,
        body: CompileSpanishCurveRequest,
        request: Request,
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        doc = _load_doc(store, project_id)
        turns = doc.get("speaker_tracks", {}).get(speaker_id, [])
        matches = [t for t in turns if t["audio_track_id"] == body.audio_track_id]
        if not 0 <= body.turn_index < len(matches):
            raise HTTPException(status_code=404, detail="Turno/pista de hablante no encontrado")
        turn = matches[body.turn_index]
        try:
            curve = compile_spanish_curve(
                curve_id=curve_id,
                speaker_id=speaker_id,
                audio_track_id=body.audio_track_id,
                text=turn["text"],
                start=turn["start"],
                end=turn["end"],
                rig_mapping=body.rig_mapping,
            )
            curve_dict = curve.to_dict()
            record = _record(
                curve_dict,
                {
                    **_SAVED_CURVE_PROVENANCE,
                    "mapping": "fixture_spanish_grapheme_to_declared_shape_key",
                    "audio_asset_id": turn["audio_asset_id"],
                    "audio_sha256": turn["audio_sha256"],
                    "identity_measurement": "fixture_only",
                    "lip_quality": "fixture_only",
                },
            )
            existing = doc["curves"].get(curve_id)
            if existing is not None and existing != record:
                raise FaceError("curva ya existe con otro contenido")
            doc["curves"][curve_id] = record
            _save_doc(store, project_id, doc)
            return {
                **curve_dict,
                "provenance": record["provenance"],
                "content_hash": record["content_hash"],
            }
        except FaceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/face/actors/{asset_id}/curves/{curve_id}/render")
    async def render_curve(
        project_id: str, asset_id: str, curve_id: str, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        source_asset = store.get_asset(project_id, asset_id)
        source = store.get_asset_path(project_id, asset_id)
        record = _load_doc(store, project_id)["curves"].get(curve_id)
        if (
            not source_asset
            or source_asset.get("kind") != "model3d"
            or source is None
            or source.suffix.lower() not in {".glb", ".gltf"}
        ):
            raise HTTPException(
                status_code=422, detail="El actor debe ser un GLB/glTF del proyecto"
            )
        if record is None:
            raise HTTPException(status_code=404, detail="Curva no encontrada")
        try:
            with tempfile.TemporaryDirectory(prefix="studio-face-") as tmp:
                output = Path(tmp) / "speaker-performance.glb"
                provenance = render_morph_glb(str(source), str(output), record["curve"])
                saved = store.save_upload(
                    project_id,
                    filename=f"{curve_id}.glb",
                    content_type="model/gltf-binary",
                    data=output.read_bytes(),
                    max_upload_bytes=256 * 1024 * 1024,
                    category="outputs",
                    metadata={
                        "speaker_curve_id": curve_id,
                        "source_actor_asset_id": asset_id,
                        "audio_track_id": record["curve"]["audio_track_id"],
                        "audio_asset_id": record["provenance"].get("audio_asset_id"),
                        "audio_sha256": record["provenance"].get("audio_sha256"),
                        "curve_provenance": record["provenance"],
                        "provenance": provenance,
                    },
                )
        except (FaceError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "asset": saved,
            "provenance": {
                **provenance,
                "quality_measurement": "fixture_only",
                "neural_tts": "not_run",
                "infinitetalk": "not_run",
            },
        }
