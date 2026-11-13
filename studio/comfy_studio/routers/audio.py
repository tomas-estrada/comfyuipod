"""Persistencia del mix causal y sus stems por proyecto (W18/A20)."""

from __future__ import annotations

import tempfile
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from comfy_studio.audio import (
    TARGET_SAMPLE_RATE,
    AudioError,
    Mix,
    SampleValidationError,
    SoundEvent,
    Stem,
    delete_cause,
    mix_digest,
    render_stems,
    validate_render_bounds,
)
from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.projects import ProjectStore

_META_KEY = "ultra_audio_mix_v1"
_STEMS_META_KEY = "ultra_audio_stems_v1"


class SoundEventRequest(BaseModel):
    event_id: str
    onset_s: float
    loudness_lufs: float = -23.0
    caused_by: str | None = None
    sample_asset_id: str | None = None


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
                        sample_asset_id=event.sample_asset_id,
                    )
                    for event in events
                ],
            )
            for name, events in body.stems.items()
        ],
    )
    mix.validate()
    return mix


def _validate_sample_assets(store: ProjectStore, project_id: str, mix: Mix) -> None:
    for event in mix.all_events():
        asset_id = event.sample_asset_id
        if asset_id is None:
            continue
        asset = store.get_asset(project_id, asset_id)
        if asset is None:
            raise HTTPException(
                status_code=400,
                detail=f"La muestra {asset_id!r} no pertenece a este proyecto",
            )
        if asset.get("kind") != "audio":
            raise HTTPException(
                status_code=400,
                detail=f"La muestra {asset_id!r} debe ser un asset de audio",
            )
        if store.get_asset_path(project_id, asset_id) is None:
            raise HTTPException(
                status_code=400,
                detail=f"La muestra {asset_id!r} no tiene un archivo accesible",
            )


def _load_render_index(store: ProjectStore, project_id: str) -> dict[str, Any]:
    raw = store.get_meta(project_id, _STEMS_META_KEY)
    return raw if isinstance(raw, dict) else {"mix_digest": None, "stems": {}}


def _mark_render_stale(
    store: ProjectStore, project_id: str, index: dict[str, Any], stem_name: str
) -> None:
    entry = index.get("stems", {}).get(stem_name)
    if not isinstance(entry, dict):
        return
    entry["stale"] = True
    asset_id = entry.get("asset_id")
    if asset_id:
        store.update_asset_metadata(
            project_id,
            str(asset_id),
            metadata={"stale": True, "stale_reason": "audio_mix_changed"},
        )


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
            _validate_sample_assets(store, project_id, mix)
            new_digest = mix_digest(mix)
            index = _load_render_index(store, project_id)
            if index.get("mix_digest") not in (None, new_digest):
                for stem_name in list(index.get("stems", {})):
                    _mark_render_stale(store, project_id, index, stem_name)
                store.set_meta(project_id, _STEMS_META_KEY, index)
            store.set_meta(project_id, _META_KEY, mix.to_dict())
            return _response(mix)
        except HTTPException:
            raise
        except AudioError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/sound/render")
    async def render_project_stems(
        project_id: str, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            mix = _load(store, project_id)
        except AudioError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if mix is None:
            raise HTTPException(status_code=404, detail="Mix no encontrado")
        try:
            validate_render_bounds(mix)
            sample_ids = sorted(
                {event.sample_asset_id for event in mix.all_events() if event.sample_asset_id}
            )
            if not sample_ids:
                raise AudioError("no hay ninguna muestra de audio para renderizar")
            _validate_sample_assets(store, project_id, mix)
            sample_paths = {
                asset_id: store.get_asset_path(project_id, asset_id) for asset_id in sample_ids
            }
            if any(path is None for path in sample_paths.values()):
                raise SampleValidationError("una muestra no tiene archivo accesible")
            config = request.app.state.config
            renderable_stems = sum(
                any(event.sample_asset_id is not None for event in stem.events)
                for stem in mix.stems
            )
            # 16-bit stereo PCM is 4 bytes/sample-frame. Bound total staged and
            # persisted output by the runtime's configured upload budget.
            estimated_bytes = int(mix.duration_s * TARGET_SAMPLE_RATE * 4 + 44) * renderable_stems
            if estimated_bytes > config.max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "stems exceden el presupuesto dinámico de salida del proyecto "
                        f"({estimated_bytes} > {config.max_upload_bytes} bytes)"
                    ),
                )
            ffmpeg_bin = find_ffmpeg()
            ffprobe_bin = find_ffprobe()
            if not ffmpeg_bin or not ffprobe_bin:
                raise HTTPException(status_code=503, detail="se requiere ffmpeg y ffprobe")
            with tempfile.TemporaryDirectory(prefix="comfy_audio_render_") as temp_dir:
                rendered = await run_in_threadpool(
                    render_stems,
                    mix,
                    sample_paths,
                    temp_dir,
                    ffmpeg_bin=ffmpeg_bin,
                    ffprobe_bin=ffprobe_bin,
                    timeout_s=config.studio_ffmpeg_timeout_s,
                )
                if not rendered:
                    raise AudioError("no hay stems con muestras de audio para renderizar")
                digest = mix_digest(mix)
                saved_entries: dict[str, dict[str, Any]] = {}
                saved_asset_ids: list[str] = []
                try:
                    for result in rendered:
                        data = result.path.read_bytes()
                        asset = store.save_upload(
                            project_id,
                            filename=f"audio-stem-{result.stem}.wav",
                            content_type="audio/wav",
                            data=data,
                            max_upload_bytes=config.max_upload_bytes,
                            category="outputs",
                            metadata={
                                "artifact_kind": "audio_rendered_stem",
                                "stem": result.stem,
                                "rendered_by": "ffmpeg",
                                "mix_digest": digest,
                                "event_ids": result.event_ids,
                                "sample_asset_ids": sorted(
                                    {
                                        event.sample_asset_id
                                        for event in mix.all_events()
                                        if event.sample_asset_id is not None
                                        and event.event_id in result.event_ids
                                    }
                                ),
                                "sample_rate": result.sample_rate,
                                "channels": result.channels,
                                "integrated_loudness_lufs": result.integrated_loudness_lufs,
                                "true_peak_dbfs": result.true_peak_dbfs,
                                "clipped": result.clipped,
                            },
                        )
                        saved_asset_ids.append(asset["id"])
                        saved_entries[result.stem] = {
                            "stem": result.stem,
                            "asset_id": asset["id"],
                            "event_ids": result.event_ids,
                            "sample_rate": result.sample_rate,
                            "channels": result.channels,
                            "duration_s": result.duration_s,
                            "integrated_loudness_lufs": result.integrated_loudness_lufs,
                            "true_peak_dbfs": result.true_peak_dbfs,
                            "clipped": result.clipped,
                            "frames": result.frames,
                            "stale": False,
                        }
                except Exception:
                    for asset_id in saved_asset_ids:
                        store.delete_asset(project_id, asset_id)
                    raise
            store.set_meta(
                project_id,
                _STEMS_META_KEY,
                {"mix_digest": digest, "stems": saved_entries},
            )
            skipped = sorted(
                stem.name for stem in mix.stems if stem.name not in saved_entries
            )
            stems = [saved_entries[name] for name in sorted(saved_entries)]
            return {
                "audio_generation": "ffmpeg_assembled",
                "separation_claim": "rendered_stems",
                "simulated": False,
                "engine": "ffmpeg",
                "stems": stems,
                "skipped_stems": skipped,
                "mix_digest": digest,
            }
        except HTTPException:
            raise
        except SampleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except AudioError as exc:
            status = 503 if "true peak" in str(exc).lower() else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/sound/stems")
    async def get_project_stems(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        index = _load_render_index(store, project_id)
        entries = [
            entry
            for _, entry in sorted(index.get("stems", {}).items())
            if isinstance(entry, dict) and store.get_asset(project_id, str(entry.get("asset_id")))
        ]
        if not entries:
            raise HTTPException(status_code=404, detail="No hay stems renderizados")
        return {"stems": entries, "mix_digest": index.get("mix_digest")}

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
        affected_stems = {
            stem.name
            for stem in mix.stems
            if any(event.caused_by == cause_id for event in stem.events)
        }
        removed = sorted(delete_cause(mix, cause_id))
        store.set_meta(project_id, _META_KEY, mix.to_dict())
        index = _load_render_index(store, project_id)
        removed_asset_ids: list[str] = []
        stale_stems: list[str] = []
        entries = index.get("stems", {})
        for stem_name in sorted(affected_stems):
            entry = entries.get(stem_name)
            if not isinstance(entry, dict):
                continue
            has_events = any(stem.name == stem_name and stem.events for stem in mix.stems)
            asset_id = entry.get("asset_id")
            if not has_events:
                if asset_id and store.delete_asset(project_id, str(asset_id)):
                    removed_asset_ids.append(str(asset_id))
                entries.pop(stem_name, None)
                continue
            entry["stale"] = True
            stale_stems.append(stem_name)
            if asset_id:
                store.update_asset_metadata(
                    project_id,
                    str(asset_id),
                    metadata={"stale": True, "stale_reason": "audio_cause_deleted"},
                )
        store.set_meta(project_id, _STEMS_META_KEY, index)
        return {
            **_response(mix),
            "removed_event_ids": removed,
            "removed_stem_asset_ids": sorted(removed_asset_ids),
            "stale_stems": stale_stems,
        }
