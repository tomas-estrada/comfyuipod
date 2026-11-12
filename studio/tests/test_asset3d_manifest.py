"""Round-trip and project ownership contracts for reusable GLB scene manifests."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.tools import (
    ROLE_TOOL_SUBSETS,
    ToolContext,
    create_default_tool_belt,
)
from comfy_studio.asset3d_manifest import get_asset3d_manifest, save_asset3d_manifest
from comfy_studio.assets3d import (
    IDENTITY_4X4,
    Asset3D,
    Asset3DError,
    CameraPose,
    Material,
    Part,
    Provenance,
)
from comfy_studio.projects import ProjectStore


def _glb() -> bytes:
    document = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [
            {
                "name": "product",
                "primitives": [{"attributes": {"POSITION": 0}, "material": 0}],
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": 3,
                "type": "VEC3",
                "min": [-1, -1, -1],
                "max": [1, 1, 1],
            }
        ],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36, "target": 34962}],
        "buffers": [{"byteLength": 36}],
        "materials": [{"name": "paint"}],
    }
    json_chunk = json.dumps(document, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    binary = struct.pack("<9f", -1, -1, -1, 1, -1, -1, 0, 1, 1)
    total_size = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return b"".join(
        (
            struct.pack("<4sII", b"glTF", 2, total_size),
            struct.pack("<II", len(json_chunk), 0x4E4F534A),
            json_chunk,
            struct.pack("<II", len(binary), 0x004E4942),
            binary,
        )
    )


def _manifest(asset_id: str) -> dict[str, Any]:
    asset = Asset3D(
        asset_id=asset_id,
        name="producto reusable",
        materials=[Material(name="paint", provenance=Provenance.OBSERVED)],
        parts=[
            Part(
                part_id="body",
                name="cuerpo observado",
                material="paint",
                provenance=Provenance.OBSERVED,
                mesh_index=0,
                node_index=0,
            ),
            Part(
                part_id="rear_detail",
                name="detalle trasero inferido",
                material="paint",
                provenance=Provenance.INFERRED,
                mesh_index=0,
                node_index=0,
                parent_part_id="body",
                transform=(
                    (1.0, 0.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0, 0.0),
                    (0.0, 0.0, 1.0, 0.5),
                    (0.0, 0.0, 0.0, 1.0),
                ),
            ),
        ],
        cameras=[
            CameraPose("front", (0.0, 0.0, 4.0), (0.0, 0.0, 0.0), provenance=Provenance.INFERRED),
            CameraPose("side", (4.0, 0.0, 0.0), (0.0, 0.0, 0.0), provenance=Provenance.INFERRED),
            CameraPose("rear", (0.0, 0.0, -4.0), (0.0, 0.0, 0.0), provenance=Provenance.INFERRED),
        ],
        transform=IDENTITY_4X4,
        source_engine="hunyuan3d_image_to_3d",
    )
    return asset.to_dict()


def _save_glb(store: ProjectStore, project_id: str, data: bytes | None = None) -> dict[str, Any]:
    return store.save_upload(
        project_id,
        filename="product.glb",
        content_type="model/gltf-binary",
        data=data if data is not None else _glb(),
        max_upload_bytes=2 * 1024 * 1024,
    )


def test_glb_manifest_reopens_same_asset_for_three_views_and_parts(
    studio_data_dir: Path,
) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project("Producto 3D")
    asset = _save_glb(store, project["id"])

    record = save_asset3d_manifest(store, project["id"], asset["id"], _manifest(asset["id"]))
    reopened = get_asset3d_manifest(store, project["id"], asset["id"])
    assert reopened == record
    model = Asset3D.from_dict(record["manifest"])
    assert model.asset_id == asset["id"]
    assert [camera.name for camera in model.cameras] == ["front", "side", "rear"]
    assert {part.part_id for part in model.parts} == {"body", "rear_detail"}
    assert model.parts[1].transform[2][3] == 0.5
    assert model.parts[1].provenance is Provenance.INFERRED
    assert model.has_inferred_geometry() is True


def test_glb_hash_change_invalidates_manifest(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project("Producto 3D")
    asset = _save_glb(store, project["id"])
    save_asset3d_manifest(store, project["id"], asset["id"], _manifest(asset["id"]))
    path = store.get_asset_path(project["id"], asset["id"])
    assert path is not None
    original = path.read_bytes()
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    with pytest.raises(Asset3DError, match="cambió"):
        get_asset3d_manifest(store, project["id"], asset["id"])


def test_stub_header_without_real_mesh_is_rejected(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project("Producto 3D")
    asset = _save_glb(store, project["id"], b"glTF\x02\x00\x00\x00\x14\x00\x00\x00\x00" * 2)
    with pytest.raises(Asset3DError, match="GLB"):
        save_asset3d_manifest(store, project["id"], asset["id"], _manifest(asset["id"]))


def test_wrong_mesh_index_is_rejected(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project("Producto 3D")
    asset = _save_glb(store, project["id"])
    manifest = _manifest(asset["id"])
    manifest["parts"][0]["mesh_index"] = 12
    with pytest.raises(Asset3DError, match="mesh GLB existente"):
        save_asset3d_manifest(store, project["id"], asset["id"], manifest)


def test_cyclic_part_hierarchy_is_rejected() -> None:
    manifest = _manifest("mesh-id")
    manifest["parts"][0]["parent_part_id"] = "rear_detail"
    with pytest.raises(Asset3DError, match="ciclo"):
        Asset3D.from_dict(manifest)


def test_manifest_api_and_agent_tool_are_real_entrypoints(client: TestClient) -> None:
    project_resp = client.post("/studio/api/projects", json={"name": "3D API"})
    project_id = project_resp.json()["id"]
    upload = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("product.glb", _glb(), "model/gltf-binary")},
    )
    assert upload.status_code == 201, upload.text
    asset_id = upload.json()["id"]
    put = client.put(
        f"/studio/api/projects/{project_id}/assets/{asset_id}/3d-manifest",
        json={"manifest": _manifest(asset_id)},
    )
    assert put.status_code == 200, put.text
    assert put.json()["model_sha256"]
    assert len(put.json()["manifest"]["cameras"]) == 3
    get = client.get(f"/studio/api/projects/{project_id}/assets/{asset_id}/3d-manifest")
    assert get.status_code == 200
    assert get.json()["manifest"]["asset_id"] == asset_id

    from comfy_studio.agent.tools import ROLE_TOOL_SUBSETS, create_default_tool_belt

    belt = create_default_tool_belt()
    assert "register_3d_manifest" in belt.list_names()
    assert "register_3d_manifest" in belt.restricted_to(ROLE_TOOL_SUBSETS["artist3d"]).list_names()


@pytest.mark.anyio
async def test_artist3d_agent_dispatch_registers_the_persisted_manifest(
    studio_data_dir: Path,
    studio_config,
    stub_comfyui_transport,
) -> None:
    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.progress import ProgressBroker

    store = ProjectStore(studio_data_dir)
    project_id = store.create_project("3D agente")["id"]
    asset = _save_glb(store, project_id)
    broker = ProgressBroker()
    comfyui_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    allowed = frozenset(ROLE_TOOL_SUBSETS["artist3d"])
    context = ToolContext(
        project_id=project_id,
        store=store,
        comfyui_client=comfyui_client,
        broker=broker,
        config=studio_config,
        allowed_tools=allowed,
    )
    belt = create_default_tool_belt().restricted_to(allowed)
    result = await belt.execute(
        "register_3d_manifest",
        {"asset_id": asset["id"], "manifest": _manifest(asset["id"])},
        context,
    )
    assert result["status"] == "registered"
    assert result["manifest"]["manifest"]["asset_id"] == asset["id"]
    assert get_asset3d_manifest(store, project_id, asset["id"]) is not None


@pytest.mark.anyio
async def test_artist3d_renders_and_persists_manifest_camera_previews(
    studio_data_dir: Path,
    studio_config,
    stub_comfyui_transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import BytesIO

    from PIL import Image

    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.engines import EngineResult
    from comfy_studio.engines.blender import BlenderEngine
    from comfy_studio.progress import ProgressBroker

    store = ProjectStore(studio_data_dir)
    project_id = store.create_project("Previews 3D")["id"]
    asset = _save_glb(store, project_id)
    save_asset3d_manifest(store, project_id, asset["id"], _manifest(asset["id"]))
    broker = ProgressBroker()
    comfyui_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    allowed = frozenset(ROLE_TOOL_SUBSETS["artist3d"])
    context = ToolContext(
        project_id=project_id,
        store=store,
        comfyui_client=comfyui_client,
        broker=broker,
        config=studio_config,
        allowed_tools=allowed,
    )
    calls: list[dict[str, Any]] = []

    async def fake_render(self, spec):
        del self
        calls.append(spec.job)
        output_dir = Path(spec.job["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        encoded = BytesIO()
        Image.new("RGB", (512, 512), (45, 90, 135)).save(encoded, format="PNG")
        outputs = []
        for index in range(3):
            path = output_dir / f"view_{index + 1:02d}.png"
            path.write_bytes(encoded.getvalue())
            outputs.append(str(path))
        return EngineResult(ok=True, engine="blender", verb="run", payload={"outputs": outputs})

    monkeypatch.setattr(BlenderEngine, "run", fake_render)
    result = (
        await create_default_tool_belt()
        .restricted_to(allowed)
        .execute("render_asset3d_views", {"asset_id": asset["id"]}, context)
    )
    assert result["status"] == "completed"
    assert result["camera_count"] == 3
    assert result["simulated"] is False
    assert calls[0]["engine"] == "cycles"
    assert calls[0]["device"] == "cpu"
    assert calls[0]["up_axis"] == "y_up"
    assert [preview["metadata"]["camera_name"] for preview in result["previews"]] == [
        "front",
        "side",
        "rear",
    ]
    assert all(preview["metadata"]["source_sha256"] for preview in result["previews"])


@pytest.mark.anyio
async def test_artist3d_staging_search_scores_real_cpu_renders_and_persists_report(
    studio_data_dir: Path,
    studio_config,
    stub_comfyui_transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    from PIL import Image, ImageDraw

    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.engines import EngineResult
    from comfy_studio.engines.blender import BlenderEngine
    from comfy_studio.progress import ProgressBroker

    store = ProjectStore(studio_data_dir)
    project_id = store.create_project("Staging CPU")["id"]
    asset = _save_glb(store, project_id)
    manifest = _manifest(asset["id"])
    for part in manifest["parts"]:
        part["bbox"] = [-1, -1, -1, 1, 1, 1]
    save_asset3d_manifest(store, project_id, asset["id"], manifest)
    broker = ProgressBroker()
    context = ToolContext(
        project_id=project_id,
        store=store,
        comfyui_client=ComfyUIClient(
            base_url=studio_config.studio_comfyui_url,
            transport=stub_comfyui_transport,
            store=store,
            broker=broker,
            redactor=studio_config.redactor(),
        ),
        broker=broker,
        config=studio_config,
        allowed_tools=frozenset(ROLE_TOOL_SUBSETS["artist3d"]),
    )
    jobs: list[dict[str, Any]] = []

    async def fake_render(self, spec):
        del self
        jobs.append(spec.job)
        output_dir = Path(spec.job["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs = []
        for index, view in enumerate(spec.job["camera_views"], start=1):
            image = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            ImageDraw.Draw(image).rectangle((160 + index, 120, 350, 390), fill=(160, 120, 80, 255))
            path = output_dir / f"view_{index:02d}_{view['name']}.png"
            image.save(path)
            outputs.append(str(path))
        return EngineResult(ok=True, engine="blender", verb="run", payload={"outputs": outputs})

    monkeypatch.setattr(BlenderEngine, "run", fake_render)
    result = (
        await create_default_tool_belt()
        .restricted_to(frozenset(ROLE_TOOL_SUBSETS["artist3d"]))
        .execute("optimize_asset3d_staging", {"asset_id": asset["id"]}, context)
    )
    assert result["status"] == "completed"
    assert result["rendered_candidates"] == 8
    assert result["simulated"] is False
    assert result["semantic_salience_evaluated"] is False
    assert result["source_sha256"]
    assert jobs[0]["device"] == "cpu"
    assert jobs[0]["transparent_background"] is True
    assert len(jobs[0]["camera_views"]) == 8
    assert result["preview"]["metadata"]["source_sha256"] == result["source_sha256"]
    report = store.get_asset_path(project_id, result["report"]["id"]).read_text()
    assert '"gpu_used": false' in report
    assert '"semantic_salience_evaluated": false' in report


@pytest.mark.anyio
async def test_artist3d_preview_fails_closed_when_blender_is_simulated(
    studio_data_dir: Path,
    studio_config,
    stub_comfyui_transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from comfy_studio.agent.tools import ToolExecutionError
    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.engines import EngineResult
    from comfy_studio.engines.blender import BlenderEngine
    from comfy_studio.progress import ProgressBroker

    store = ProjectStore(studio_data_dir)
    project_id = store.create_project("Previews 3D stub")["id"]
    asset = _save_glb(store, project_id)
    save_asset3d_manifest(store, project_id, asset["id"], _manifest(asset["id"]))
    broker = ProgressBroker()
    context = ToolContext(
        project_id=project_id,
        store=store,
        comfyui_client=ComfyUIClient(
            base_url=studio_config.studio_comfyui_url,
            transport=stub_comfyui_transport,
            store=store,
            broker=broker,
            redactor=studio_config.redactor(),
        ),
        broker=broker,
        config=studio_config,
        allowed_tools=frozenset(ROLE_TOOL_SUBSETS["artist3d"]),
    )

    async def fake_stub(self, spec):
        del self, spec
        return EngineResult(
            ok=True,
            engine="blender",
            verb="run",
            payload={"outputs": []},
            simulated=True,
        )

    monkeypatch.setattr(BlenderEngine, "run", fake_stub)
    with pytest.raises(ToolExecutionError, match="stub no es evidencia"):
        await create_default_tool_belt().execute(
            "render_asset3d_views", {"asset_id": asset["id"]}, context
        )
