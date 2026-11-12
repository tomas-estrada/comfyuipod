"""Project-owned, hash-bound glTF manifests for reusable 3D assets."""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

from comfy_studio.assets3d import Asset3D, Asset3DError

MANIFEST_SCHEMA = "comfy_studio.asset3d_manifest.v1"
_META_PREFIX = "asset3d_manifest:"
_MAX_JSON_CHUNK_BYTES = 16 * 1024 * 1024
_GLB_MAGIC = b"glTF"
_JSON_CHUNK = 0x4E4F534A
_BIN_CHUNK = 0x004E4942


def _read_glb(path: Path) -> tuple[dict[str, Any], str]:
    """Validate a GLB v2's JSON/BIN structure and return JSON plus mesh counts."""
    size = path.stat().st_size
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise Asset3DError("GLB incompleto: falta la cabecera")
        magic, version, declared_size = struct.unpack("<4sII", header)
        if magic != _GLB_MAGIC or version != 2 or declared_size != size:
            raise Asset3DError("el asset no es un GLB v2 íntegro")
        chunk_header = stream.read(8)
        if len(chunk_header) != 8:
            raise Asset3DError("GLB incompleto: falta el chunk JSON")
        json_size, json_kind = struct.unpack("<II", chunk_header)
        if json_kind != _JSON_CHUNK or json_size > _MAX_JSON_CHUNK_BYTES:
            raise Asset3DError("chunk JSON GLB inválido o demasiado grande")
        raw_json = stream.read(json_size)
        if len(raw_json) != json_size:
            raise Asset3DError("chunk JSON GLB truncado")
        try:
            document = json.loads(raw_json.rstrip(b"\x00 \t\r\n").decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Asset3DError(f"JSON GLB inválido: {exc}") from exc
        asset_header = document.get("asset") if isinstance(document, dict) else None
        if not isinstance(asset_header, dict) or asset_header.get("version") != "2.0":
            raise Asset3DError("el GLB no declara asset.version 2.0")
        meshes = document.get("meshes") or []
        nodes = document.get("nodes") or []
        accessors = document.get("accessors") or []
        views = document.get("bufferViews") or []
        buffers = document.get("buffers") or []
        position_accessors: set[int] = set()
        for mesh_index, mesh in enumerate(meshes):
            primitives = mesh.get("primitives") or []
            if not primitives:
                raise Asset3DError(f"mesh {mesh_index} no contiene primitives")
            for primitive in primitives:
                accessor_index = (primitive.get("attributes") or {}).get("POSITION")
                if not isinstance(accessor_index, int) or accessor_index < 0:
                    raise Asset3DError(f"mesh {mesh_index} carece de POSITION indexado")
                if accessor_index >= len(accessors):
                    raise Asset3DError(f"mesh {mesh_index} referencia un accessor inexistente")
                accessor = accessors[accessor_index]
                if accessor.get("type") != "VEC3" or int(accessor.get("count") or 0) < 1:
                    raise Asset3DError(f"mesh {mesh_index} POSITION no tiene vértices VEC3")
                view_index = accessor.get("bufferView")
                if not isinstance(view_index, int) or not 0 <= view_index < len(views):
                    raise Asset3DError(f"mesh {mesh_index} POSITION no tiene bufferView válido")
                view = views[view_index]
                buffer_index = view.get("buffer")
                if not isinstance(buffer_index, int) or not 0 <= buffer_index < len(buffers):
                    raise Asset3DError(f"mesh {mesh_index} referencia un buffer inexistente")
                component_bytes = {
                    5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4,
                }.get(int(accessor.get("componentType") or 0))
                if component_bytes is None:
                    raise Asset3DError(f"mesh {mesh_index} usa componentType no válido")
                element_bytes = component_bytes * 3
                stride = int(view.get("byteStride") or element_bytes)
                accessor_end = int(accessor.get("byteOffset") or 0) + (
                    int(accessor["count"]) - 1
                ) * stride + element_bytes
                if stride < element_bytes or accessor_end > int(view.get("byteLength") or 0):
                    raise Asset3DError(f"mesh {mesh_index} POSITION excede el bufferView")
                position_accessors.add(accessor_index)
        if not meshes or not position_accessors:
            raise Asset3DError("el GLB no contiene geometría de malla verificable")
        declared_buffers = sum(int(buf.get("byteLength") or 0) for buf in buffers)
        if declared_buffers <= 0:
            raise Asset3DError("el GLB declara buffers vacíos")
        bin_header = stream.read(8)
        if len(bin_header) != 8:
            raise Asset3DError("GLB sin chunk binario de geometría")
        bin_size, bin_kind = struct.unpack("<II", bin_header)
        if bin_kind != _BIN_CHUNK or bin_size < declared_buffers:
            raise Asset3DError("chunk BIN GLB ausente o menor al buffer declarado")
        if stream.tell() + bin_size > size:
            raise Asset3DError("chunk BIN GLB truncado")
        for view in views:
            buffer_index = int(view.get("buffer", -1))
            if not 0 <= buffer_index < len(buffers):
                raise Asset3DError("bufferView referencia un buffer inexistente")
            view_end = int(view.get("byteOffset") or 0) + int(view.get("byteLength") or 0)
            if view_end > int(buffers[buffer_index].get("byteLength") or 0):
                raise Asset3DError("bufferView excede los bytes del buffer")

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    document["_comfy_manifest_mesh_count"] = len(meshes)
    document["_comfy_manifest_node_count"] = len(nodes)
    return document, digest.hexdigest()


def save_asset3d_manifest(
    store: Any,
    project_id: str,
    asset_id: str,
    manifest_data: dict[str, Any],
) -> dict[str, Any]:
    """Persist a reusable scene manifest bound to an owned, verifiable GLB."""
    asset = store.get_asset(project_id, asset_id)
    path = store.get_asset_path(project_id, asset_id)
    if asset is None or path is None:
        raise LookupError("el GLB no existe en este proyecto")
    if asset.get("kind") != "model3d" or path.suffix.lower() != ".glb":
        raise Asset3DError("el manifiesto solo puede vincularse a un asset GLB del proyecto")
    gltf, digest = _read_glb(path)
    model = Asset3D.from_dict(manifest_data)
    if model.asset_id != asset_id:
        raise Asset3DError("asset_id del manifiesto debe coincidir con el GLB del proyecto")
    mesh_count = int(gltf["_comfy_manifest_mesh_count"])
    node_count = int(gltf["_comfy_manifest_node_count"])
    gltf_nodes = gltf.get("nodes") or []
    gltf_materials = gltf.get("materials") or []
    if not model.parts:
        raise Asset3DError("el manifiesto requiere al menos una parte identificada")
    if not model.cameras:
        raise Asset3DError("el manifiesto requiere al menos una cámara reabrible")
    for part in model.parts:
        if part.mesh_index is None or part.mesh_index >= mesh_count:
            raise Asset3DError(f"parte {part.part_id} debe apuntar a un mesh GLB existente")
        if part.node_index is not None and part.node_index >= node_count:
            raise Asset3DError(f"parte {part.part_id} apunta a un node GLB inexistente")
        if (
            part.node_index is not None
            and gltf_nodes[part.node_index].get("mesh") != part.mesh_index
        ):
            raise Asset3DError(
                f"parte {part.part_id}: node_index y mesh_index no describen el mismo objeto"
            )
    gltf_material_names = {
        int(index): str(material.get("name"))
        for index, material in enumerate(gltf_materials)
        if material.get("name")
    }
    for material in model.materials:
        material_index = material.material_index
        if material_index is None:
            material_index = next(
                (index for index, name in gltf_material_names.items() if name == material.name),
                None,
            )
        if material_index is None or not 0 <= material_index < len(gltf_materials):
            raise Asset3DError(
                f"material {material.name} debe apuntar a un material GLB existente"
            )
        gltf_name = gltf_material_names.get(material_index)
        if gltf_name and gltf_name != material.name:
            raise Asset3DError(f"material {material.name} no coincide con el nombre glTF")
        material.material_index = material_index
    record = {
        "schema": MANIFEST_SCHEMA,
        "asset_id": asset_id,
        "project_id": project_id,
        "model_filename": asset.get("original_filename"),
        "model_sha256": digest,
        "mesh_count": mesh_count,
        "node_count": node_count,
        "manifest": model.to_dict(),
    }
    encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise Asset3DError("manifiesto supera el límite de 1 MiB")
    store.set_meta(project_id, f"{_META_PREFIX}{asset_id}", record)
    return record


def get_asset3d_manifest(store: Any, project_id: str, asset_id: str) -> dict[str, Any] | None:
    """Reopen a manifest only while its project-owned GLB hash still matches."""
    record = store.get_meta(project_id, f"{_META_PREFIX}{asset_id}")
    if not isinstance(record, dict):
        return None
    path = store.get_asset_path(project_id, asset_id)
    if path is None:
        raise LookupError("el GLB del manifiesto ya no está disponible en este proyecto")
    _gltf, digest = _read_glb(path)
    if digest != record.get("model_sha256"):
        raise Asset3DError("el GLB cambió; el manifiesto debe revisarse antes de reabrirlo")
    Asset3D.from_dict(record.get("manifest") or {})
    return record


__all__ = ["MANIFEST_SCHEMA", "get_asset3d_manifest", "save_asset3d_manifest"]
