from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comfy_studio.agent.tools import (
    SimulatePhysicsArgs,
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
)
from comfy_studio.blender_physics import (
    BlenderPhysicsError,
    BlenderPhysicsSpec,
    _physics_state_hash,
    build_physics_script,
)
from comfy_studio.config import StudioConfig
from comfy_studio.projects import ProjectStore


def _scene(tmp_path: Path) -> Path:
    path = tmp_path / "scene.glb"
    path.write_bytes(b"glTF\x02\x00\x00\x00")
    return path


def test_physics_spec_is_bounded_and_requires_a_ground_object(tmp_path: Path) -> None:
    spec = BlenderPhysicsSpec(
        scene_path=_scene(tmp_path),
        output_dir=tmp_path / "output",
        moving_object="body",
        ground_object="ground",
    )
    spec.validate()
    with pytest.raises(BlenderPhysicsError, match="ground_object"):
        BlenderPhysicsSpec(
            scene_path=spec.scene_path,
            output_dir=spec.output_dir,
            moving_object="body",
        ).validate()
    with pytest.raises(BlenderPhysicsError, match="duration_s"):
        BlenderPhysicsSpec(
            scene_path=spec.scene_path,
            output_dir=spec.output_dir,
            moving_object="body",
            ground_object="ground",
            fps=60,
            duration_s=10.1,
        ).validate()


def test_generated_physics_script_parses_and_contains_provenance(tmp_path: Path) -> None:
    spec = BlenderPhysicsSpec(
        scene_path=_scene(tmp_path),
        output_dir=tmp_path / "output",
        moving_object="body",
        ground_object="ground",
    )
    script = build_physics_script(
        spec,
        spec.output_dir / "result.json",
        spec.output_dir / "scene.glb",
    )
    compile(script, "blender_physics_job.py", "exec")
    assert "Blender Bullet rigid-body world" in script
    assert "state_hash" in script
    assert "evaluated_mesh_proximity_after_bullet_step" in script


def test_physics_state_hash_covers_trace_payload() -> None:
    payload = {"schema": "comfy_studio.blender-physics.v1", "frames": [{"frame": 1}]}
    payload["state_hash"] = _physics_state_hash(payload)
    assert payload["state_hash"] == _physics_state_hash(payload)
    payload["frames"][0]["frame"] = 2
    assert payload["state_hash"] != _physics_state_hash(payload)


def test_hinge_spec_validates_joint_and_generates_limit_checks(tmp_path: Path) -> None:
    spec = BlenderPhysicsSpec(
        scene_path=_scene(tmp_path),
        output_dir=tmp_path / "hinge-output",
        moving_object="link",
        maneuver="hinge",
        fixed_object="anchor",
        hinge_pivot_m=(0.0, 0.0, 1.5),
        hinge_axis=(0.0, 1.0, 0.0),
        hinge_lower_limit_deg=-20.0,
        hinge_upper_limit_deg=30.0,
    )
    spec.validate()
    script = build_physics_script(
        spec,
        spec.output_dir / "result.json",
        spec.output_dir / "scene.glb",
    )
    compile(script, "blender_hinge_job.py", "exec")
    assert 'constraint_add(type="HINGE")' in script
    assert '"joint_limit_ok"' in script
    assert '"reset_pose_delta_m"' in script

    with pytest.raises(BlenderPhysicsError, match="fixed_object"):
        BlenderPhysicsSpec(
            scene_path=spec.scene_path,
            output_dir=spec.output_dir,
            moving_object="link",
            maneuver="hinge",
            hinge_pivot_m=(0.0, 0.0, 1.5),
        ).validate()
    with pytest.raises(BlenderPhysicsError, match="vectores 3D"):
        BlenderPhysicsSpec(
            scene_path=spec.scene_path,
            output_dir=spec.output_dir,
            moving_object="link",
            maneuver="hinge",
            fixed_object="anchor",
            hinge_pivot_m=(0.0, 0.0, 1.5),
            hinge_axis=(0.0, 0.0, 0.0),
        ).validate()
    with pytest.raises(BlenderPhysicsError, match="deben ser distintos"):
        BlenderPhysicsSpec(
            scene_path=spec.scene_path,
            output_dir=spec.output_dir,
            moving_object="link",
            maneuver="hinge",
            fixed_object="link",
            hinge_pivot_m=(0.0, 0.0, 1.5),
        ).validate()


def test_simulate_physics_tool_is_registered_and_parameters_are_bounded() -> None:
    belt = create_default_tool_belt()
    spec = belt.get("simulate_physics")
    assert spec is not None
    assert spec.args_model is SimulatePhysicsArgs
    with pytest.raises(ValueError):
        SimulatePhysicsArgs(
            asset_id="a",
            moving_object="body",
            ground_object="ground",
            duration_s=11,
        )
    hinge = SimulatePhysicsArgs(
        asset_id="a",
        moving_object="link",
        maneuver="hinge",
        fixed_object="anchor",
        hinge_pivot_m=(0, 0, 1),
        hinge_axis=(0, 1, 0),
        hinge_lower_limit_deg=-20,
        hinge_upper_limit_deg=20,
    )
    assert hinge.ground_object is None
    with pytest.raises(ValueError, match="ground_object"):
        SimulatePhysicsArgs(asset_id="a", moving_object="body")
    with pytest.raises(ValueError, match="fixed_object"):
        SimulatePhysicsArgs(asset_id="a", moving_object="link", maneuver="hinge")
    with pytest.raises(ValueError, match="deben ser distintos"):
        SimulatePhysicsArgs(
            asset_id="a",
            moving_object="link",
            maneuver="hinge",
            fixed_object="link",
            hinge_pivot_m=(0, 0, 1),
        )


def test_simulate_physics_rejects_stub_and_non_3d_assets(
    studio_data_dir: Path,
    studio_config: StudioConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="physics")
    image = store.save_upload(
        project["id"],
        filename="image.png",
        content_type="image/png",
        data=b"png",
        max_upload_bytes=studio_config.max_upload_bytes,
    )
    context = ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=None,
        broker=None,
        config=studio_config,
    )
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="asset 3D"):
        asyncio.run(
            belt.execute(
                "simulate_physics",
                {"asset_id": image["id"], "moving_object": "body", "ground_object": "ground"},
                context,
            )
        )

    model = store.save_upload(
        project["id"],
        filename="scene.glb",
        content_type="model/gltf-binary",
        data=b"glTF\x02\x00\x00\x00",
        max_upload_bytes=studio_config.max_upload_bytes,
    )
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    with pytest.raises(ToolExecutionError, match="modo stub no es evidencia"):
        asyncio.run(
            belt.execute(
                "simulate_physics",
                {"asset_id": model["id"], "moving_object": "body", "ground_object": "ground"},
                context,
            )
        )


def test_project_store_accepts_json_physics_trace(
    studio_data_dir: Path,
    studio_config: StudioConfig,
) -> None:
    store = ProjectStore(studio_data_dir)
    project = store.create_project(name="trace")
    saved = store.save_upload(
        project["id"],
        filename="trace.json",
        content_type="application/json",
        data=b'{"schema":"comfy_studio.blender-physics.v1"}',
        max_upload_bytes=studio_config.max_upload_bytes,
        category="outputs",
    )
    assert saved["kind"] == "data"
    assert saved["filename"].endswith(".json")
