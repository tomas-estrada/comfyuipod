"""Movimiento persistido por proyecto (C11-C14 / A15).

Expone el contrato determinista de `comfy_studio.motion` en el API de Studio:
guardado y validación de esqueletos y clips, retarget determinista en CPU y
export/import reejecutable, todo persistido en los metadatos del proyecto
(ProjectStore) y acotado a su propiedad. Conserva raíz, timebase, contactos,
límites de articulación y los canales identidad/actuación/máscara/cámara.

A15: la procedencia declara explícitamente que no hay generación neural de
movimiento ni captura de actuación, y que Isaac Gym/ReMoGen no están
disponibles — no se fingen bajo un comando nominalmente integrado.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from comfy_studio.motion import (
    Bone,
    MotionClip,
    MotionError,
    Skeleton,
)
from comfy_studio.motion import (
    retarget as motion_retarget,
)
from comfy_studio.motion_cpu import compose_actions, recover_motion, transfer_acting
from comfy_studio.projects import ProjectStore

_META_KEY = "ultra_motion_v1"
_EXPORT_SCHEMA = "ultra-motion-export-v1"

#: Declaración honesta de motores y capacidades (A15): sin GPU, sin modelos
#: neuronales y sin captura; el retarget disponible es el determinista en CPU.
_ENGINES_DECLARATION: dict[str, Any] = {
    "isaac_gym": "unavailable",
    "remogen": "unavailable",
    "retarget_engine": "cpu-deterministic-rules",
    "neural_generation": "not_used",
    "performance_capture": "not_performed",
    "gpu_validation": "not_run",
}

_SAVED_CLIP_PROVENANCE = {
    "origin": "declared_keyframes",
    "neural_generation": "not_used",
    "performance_capture": "not_performed",
}


class BoneModel(BaseModel):
    name: str
    parent: str | None = None
    min_deg: float = -180.0
    max_deg: float = 180.0


class SkeletonRequest(BaseModel):
    name: str
    root: str
    scale: float = 1.0
    #: bind pose declarada en grados por hueso; vacío ⇒ bind efectiva 0.0.
    bind: dict[str, float] = Field(default_factory=dict)
    body_class: str = "humanoid"
    identity_id: str | None = None
    bones: list[BoneModel]


class ClipRequest(BaseModel):
    skeleton_id: str
    clip: dict[str, Any]


class RetargetRequest(BaseModel):
    target_skeleton_id: str
    mapping: dict[str, str]
    clip_id: str


def _store(request: Request) -> ProjectStore:
    return request.app.state.store


def _require_project(store: ProjectStore, project_id: str) -> None:
    if store.get_project(project_id) is None:
        raise HTTPException(status_code=404, detail="Proyecto no encontrado")


def _load_doc(store: ProjectStore, project_id: str) -> dict[str, Any]:
    raw = store.get_meta(project_id, _META_KEY)
    doc = raw if isinstance(raw, dict) else {}
    return {
        "skeletons": doc.get("skeletons", {}),
        "clips": doc.get("clips", {}),
    }


def _save_doc(store: ProjectStore, project_id: str, doc: dict[str, Any]) -> None:
    store.set_meta(project_id, _META_KEY, doc)


def _skeleton_to_dict(skeleton: Skeleton) -> dict[str, Any]:
    result = {
        "name": skeleton.name,
        "root": skeleton.root,
        "scale": skeleton.scale,
        "body_class": skeleton.body_class,
        "identity_id": skeleton.identity_id,
        "bones": [
            {
                "name": bone.name,
                "parent": bone.parent,
                "min_deg": bone.min_deg,
                "max_deg": bone.max_deg,
            }
            for bone in skeleton.bones
        ],
    }
    if skeleton.bind:
        result["bind"] = dict(skeleton.bind)
    return result


def _skeleton_from_dict(data: dict[str, Any]) -> Skeleton:
    skeleton = Skeleton(
        name=data["name"],
        root=data["root"],
        scale=float(data.get("scale", 1.0)),
        bind={str(bone): float(degrees) for bone, degrees in data.get("bind", {}).items()},
        body_class=str(data.get("body_class", "humanoid")),
        identity_id=data.get("identity_id"),
        bones=[
            Bone(
                name=bone["name"],
                parent=bone.get("parent"),
                min_deg=float(bone.get("min_deg", -180.0)),
                max_deg=float(bone.get("max_deg", 180.0)),
            )
            for bone in data.get("bones", [])
        ],
    )
    skeleton.validate()
    return skeleton


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(skeleton_dict: dict[str, Any], clip_dict: dict[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({"skeleton": skeleton_dict, "clip": clip_dict}).encode("utf-8")
    ).hexdigest()


def _clip_record(
    clip: MotionClip,
    skeleton_id: str,
    provenance: dict[str, Any],
    skeleton_dict: dict[str, Any],
) -> dict[str, Any]:
    clip_dict = clip.to_dict()
    return {
        "skeleton_id": skeleton_id,
        "clip": clip_dict,
        "provenance": provenance,
        "content_hash": _content_hash(skeleton_dict, clip_dict),
    }


def _clip_response(record: dict[str, Any]) -> dict[str, Any]:
    return {**record, "gpu_validation": "not_run"}


def _get_skeleton(doc: dict[str, Any], skeleton_id: str) -> Skeleton:
    raw = doc["skeletons"].get(skeleton_id)
    if raw is None:
        raise MotionError(f"esqueleto inexistente {skeleton_id!r}")
    return _skeleton_from_dict(raw)


def _persist_clip(
    store: ProjectStore,
    project_id: str,
    clip_id: str,
    record: dict[str, Any],
    doc: dict[str, Any],
) -> None:
    """Guarda idempotente: reintentos idénticos pasan; conflictos fallan (409)."""
    existing = doc["clips"].get(clip_id)
    if existing is not None and existing != record:
        raise MotionError(f"clip {clip_id!r} ya existe con contenido distinto; usa otro id")
    doc["clips"][clip_id] = record
    _save_doc(store, project_id, doc)


def register_routes(app: FastAPI) -> None:
    """Expone movimiento persistido por proyecto en /studio/api."""

    @app.put("/studio/api/projects/{project_id}/motion/skeletons/{skeleton_id}")
    async def put_skeleton(
        project_id: str, skeleton_id: str, body: SkeletonRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            skeleton = Skeleton(
                name=body.name,
                root=body.root,
                scale=body.scale,
                bind=dict(body.bind),
                body_class=body.body_class,
                identity_id=body.identity_id,
                bones=[
                    Bone(
                        name=bone.name,
                        parent=bone.parent,
                        min_deg=bone.min_deg,
                        max_deg=bone.max_deg,
                    )
                    for bone in body.bones
                ],
            )
            skeleton.validate()
            skeleton_dict = _skeleton_to_dict(skeleton)
            doc = _load_doc(store, project_id)
            existing = doc["skeletons"].get(skeleton_id)
            if existing is not None and existing != skeleton_dict:
                raise MotionError(
                    f"esqueleto {skeleton_id!r} ya existe con contenido distinto; usa otro id"
                )
            if existing is None:
                doc["skeletons"][skeleton_id] = skeleton_dict
                _save_doc(store, project_id, doc)
            return {**skeleton_dict, "gpu_validation": "not_run"}
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/motion/skeletons")
    async def list_skeletons(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        return {"skeletons": _load_doc(store, project_id)["skeletons"]}

    @app.get("/studio/api/projects/{project_id}/motion/skeletons/{skeleton_id}")
    async def get_skeleton(project_id: str, skeleton_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            skeleton_dict = _load_doc(store, project_id)["skeletons"][skeleton_id]
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail=f"Esqueleto no encontrado: {skeleton_id}"
            ) from exc
        return {**skeleton_dict, "gpu_validation": "not_run"}

    @app.put("/studio/api/projects/{project_id}/motion/clips/{clip_id}")
    async def put_clip(
        project_id: str, clip_id: str, body: ClipRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            doc = _load_doc(store, project_id)
            skeleton = _get_skeleton(doc, body.skeleton_id)
            clip = MotionClip.from_dict(body.clip)
            clip.validate(skeleton)
            record = _clip_record(
                clip, body.skeleton_id, dict(_SAVED_CLIP_PROVENANCE), _skeleton_to_dict(skeleton)
            )
            _persist_clip(store, project_id, clip_id, record, doc)
            return _clip_response(record)
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/motion/clips")
    async def list_clips(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        return {"clips": _load_doc(store, project_id)["clips"]}

    @app.get("/studio/api/projects/{project_id}/motion/clips/{clip_id}")
    async def get_clip(project_id: str, clip_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        record = _load_doc(store, project_id)["clips"].get(clip_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Clip no encontrado: {clip_id}")
        return _clip_response(record)

    @app.post("/studio/api/projects/{project_id}/motion/clips/{clip_id}/retarget")
    async def retarget_clip(
        project_id: str, clip_id: str, body: RetargetRequest, request: Request
    ) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            doc = _load_doc(store, project_id)
            record = doc["clips"].get(clip_id)
            if record is None:
                raise MotionError(f"clip inexistente {clip_id!r}")
            source = _get_skeleton(doc, record["skeleton_id"])
            target = _get_skeleton(doc, body.target_skeleton_id)
            clip = MotionClip.from_dict(record["clip"])
            retargeted = motion_retarget(clip, source, target, body.mapping)
            provenance = {
                "origin": "deterministic_retarget",
                "source_clip_id": clip_id,
                "source_skeleton_id": record["skeleton_id"],
                "target_skeleton_id": body.target_skeleton_id,
                "mapping": dict(body.mapping),
                "neural_generation": "not_used",
                "performance_capture": "not_performed",
            }
            new_record = _clip_record(
                retargeted,
                body.target_skeleton_id,
                provenance,
                _skeleton_to_dict(target),
            )
            _persist_clip(store, project_id, body.clip_id, new_record, doc)
            return {
                **new_record,
                "engines": dict(_ENGINES_DECLARATION),
                "gpu_validation": "not_run",
            }
        except MotionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/motion/clips/{clip_id}/export")
    async def export_clip(project_id: str, clip_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        record = _load_doc(store, project_id)["clips"].get(clip_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Clip no encontrado: {clip_id}")
        return {
            "schema": _EXPORT_SCHEMA,
            "origin": {
                "project_id": project_id,
                "clip_id": clip_id,
                "skeleton_id": record["skeleton_id"],
            },
            "skeleton": _load_doc(store, project_id)["skeletons"][record["skeleton_id"]],
            "clip": record["clip"],
            "provenance": record["provenance"],
            "engines": dict(_ENGINES_DECLARATION),
            "content_hash": record["content_hash"],
        }

    @app.post("/studio/api/projects/{project_id}/motion/import")
    async def import_clip(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise MotionError("documento de importación: se esperaba un objeto JSON")
            if body.get("schema") != _EXPORT_SCHEMA:
                raise MotionError("documento de importación: schema desconocido")
            skeleton_dict = body.get("skeleton")
            clip_dict = body.get("clip")
            if not isinstance(skeleton_dict, dict) or not isinstance(clip_dict, dict):
                raise MotionError("documento de importación requiere skeleton y clip")
            skeleton_id = body["origin"]["skeleton_id"]
            clip_id = body["origin"]["clip_id"]
            skeleton = _skeleton_from_dict(skeleton_dict)
            clip = MotionClip.from_dict(clip_dict)
            clip.validate(skeleton)
            normalized_skeleton = _skeleton_to_dict(skeleton)
            normalized_clip = clip.to_dict()
            expected_hash = _content_hash(normalized_skeleton, normalized_clip)
            if body.get("content_hash") != expected_hash:
                raise MotionError(
                    "documento de importación: content_hash no coincide (documento "
                    "alterado o no canónico)"
                )
            doc = _load_doc(store, project_id)
            existing = doc["skeletons"].get(skeleton_id)
            if existing is not None and existing != normalized_skeleton:
                raise MotionError(f"esqueleto {skeleton_id!r} ya existe con contenido distinto")
            doc["skeletons"][skeleton_id] = normalized_skeleton
            origin = body["origin"]
            provenance = {
                **dict(body.get("provenance", {})),
                "source_project_id": origin["project_id"],
                "imported_from_schema": _EXPORT_SCHEMA,
            }
            record = {
                "skeleton_id": skeleton_id,
                "clip": normalized_clip,
                "provenance": provenance,
                "content_hash": expected_hash,
            }
            _persist_clip(store, project_id, clip_id, record, doc)
            return {**record, "gpu_validation": "not_run"}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise HTTPException(
                status_code=409,
                detail="documento de importación: JSON malformado",
            ) from exc
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/studio/api/projects/{project_id}/motion/engines")
    async def engine_status(project_id: str, request: Request) -> dict[str, Any]:
        store = _store(request)
        _require_project(store, project_id)
        return dict(_ENGINES_DECLARATION)

    @app.post("/studio/api/projects/{project_id}/motion/skeletons/{skeleton_id}/build")
    async def build_rig(project_id: str, skeleton_id: str, request: Request) -> dict[str, Any]:
        """Create and save a real editable armature GLB using headless Blender CPU."""
        import tempfile
        from pathlib import Path

        from comfy_studio.blender_motion import BlenderRigError, BlenderRigSpec, run_blender_rig

        store = _store(request)
        _require_project(store, project_id)
        doc = _load_doc(store, project_id)
        try:
            skeleton = _get_skeleton(doc, skeleton_id)
            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("clip"), dict):
                raise MotionError("build requiere clip JSON")
            clip = MotionClip.from_dict(body["clip"])
            clip.validate(skeleton)
            config = request.app.state.config
            with tempfile.TemporaryDirectory(prefix="comfy_motion_rig_") as temporary:
                result = await run_blender_rig(
                    BlenderRigSpec(skeleton=skeleton, clip=clip, output_dir=Path(temporary))
                )
                if not result.ok or result.glb_path is None:
                    raise BlenderRigError(result.error or "Blender rig build failed")
                saved = store.save_upload(
                    project_id,
                    filename=f"{skeleton_id}_{clip.name}_rig.glb",
                    content_type="model/gltf-binary",
                    data=result.glb_path.read_bytes(),
                    max_upload_bytes=config.max_upload_bytes,
                    category="outputs",
                    metadata={
                        "engine": "blender",
                        "mode": "cpu_rig_deformation",
                        "skeleton_id": skeleton_id,
                        "clip_name": clip.name,
                        "gpu_validation": "not_run",
                        "rig_report": result.report,
                    },
                )
            return {
                "asset": saved,
                "rig": result.report,
                "engine": "blender_cpu",
                "gpu_validation": "not_run",
            }
        except (KeyError, TypeError, ValueError, MotionError, BlenderRigError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/motion/recover")
    async def recover_clip(project_id: str, request: Request) -> dict[str, Any]:
        """Persist camera/scale compensated motion from authorized 3D observations."""
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
            doc = _load_doc(store, project_id)
            skeleton_id = str(body["skeleton_id"])
            skeleton = _get_skeleton(doc, skeleton_id)
            clip = recover_motion(
                name=str(body["name"]),
                fps=int(body["fps"]),
                frames=body["frames"],
                camera_track=[tuple(point) for point in body["camera_track"]],
                camera_yaw_radians=body.get("camera_yaw_radians"),
                subject_scale=float(body["subject_scale"]),
            )
            clip.validate(skeleton)
            record = _clip_record(
                clip,
                skeleton_id,
                {
                    "origin": "declared_3d_observations",
                    "neural_generation": "not_used",
                    "performance_capture": "not_performed",
                    "camera_compensation": "translation_rotation_scale",
                },
                _skeleton_to_dict(skeleton),
            )
            _persist_clip(store, project_id, str(body["clip_id"]), record, doc)
            return _clip_response(record)
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/motion/choreography")
    async def compose_choreography(project_id: str, request: Request) -> dict[str, Any]:
        """Persist exactly three compatible actions aligned to declared beat frames."""
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
            doc = _load_doc(store, project_id)
            records = [doc["clips"][clip_id] for clip_id in body["clip_ids"]]
            skeleton_ids = {record["skeleton_id"] for record in records}
            if len(skeleton_ids) != 1:
                raise MotionError("choreography actions must use the same skeleton")
            skeleton_id = records[0]["skeleton_id"]
            skeleton = _get_skeleton(doc, skeleton_id)
            actions = [MotionClip.from_dict(record["clip"]) for record in records]
            clip = compose_actions(actions, [int(frame) for frame in body["beat_frames"]])
            clip.validate(skeleton)
            result = _clip_record(
                clip,
                skeleton_id,
                {
                    "origin": "beat_aligned_action_composition",
                    "source_clip_ids": list(body["clip_ids"]),
                    "beat_frames": list(body["beat_frames"]),
                    "neural_generation": "not_used",
                },
                _skeleton_to_dict(skeleton),
            )
            _persist_clip(store, project_id, str(body["clip_id"]), result, doc)
            return _clip_response(result)
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/studio/api/projects/{project_id}/motion/clips/{clip_id}/transfer")
    async def transfer_clip(project_id: str, clip_id: str, request: Request) -> dict[str, Any]:
        """Identity/mask-safe acting transfer between compatible body classes."""
        store = _store(request)
        _require_project(store, project_id)
        try:
            body = await request.json()
            doc = _load_doc(store, project_id)
            source_record = doc["clips"].get(clip_id)
            if source_record is None:
                raise MotionError(f"clip inexistente {clip_id!r}")
            source = _get_skeleton(doc, source_record["skeleton_id"])
            target_id = str(body["target_skeleton_id"])
            target = _get_skeleton(doc, target_id)
            clip = transfer_acting(
                MotionClip.from_dict(source_record["clip"]), source, target, body["mapping"]
            )
            output = _clip_record(
                clip,
                target_id,
                {
                    "origin": "identity_mask_safe_acting_transfer",
                    "source_clip_id": clip_id,
                    "source_skeleton_id": source_record["skeleton_id"],
                    "target_skeleton_id": target_id,
                    "mapping": dict(body["mapping"]),
                    "neural_generation": "not_used",
                },
                _skeleton_to_dict(target),
            )
            _persist_clip(store, project_id, str(body["clip_id"]), output, doc)
            return _clip_response(output)
        except (KeyError, TypeError, ValueError, MotionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
