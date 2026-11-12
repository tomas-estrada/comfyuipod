#!/usr/bin/env python3
"""Run and validate a real headless Blender Bullet free-fall/contact smoke."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path

from comfy_studio.blender_physics import BlenderPhysicsSpec, run_blender_physics_spec


def _make_scene(blender: str, scene_path: Path, work: Path) -> None:
    script = work / "make_physics_scene.py"
    script.write_text(
        """import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_plane_add(size=8, location=(0, 0, 0))
ground = bpy.context.object
ground.name = 'physics-smoke-ground'
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 1.25))
body = bpy.context.object
body.name = 'physics-smoke-body'
bpy.ops.wm.save_as_mainfile(filepath=SCENE_PATH)
""".replace("SCENE_PATH", repr(str(scene_path))),
        encoding="utf-8",
    )
    result = subprocess.run(
        [blender, "--background", "--factory-startup", "--python", str(script)],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode or not scene_path.is_file():
        raise RuntimeError(
            "Blender could not prepare the physics smoke scene: "
            f"exit={result.returncode}; {result.stderr[-1200:]} {result.stdout[-1200:]}"
        )


def _glb_json(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 20 or data[:4] != b"glTF":
        raise RuntimeError("physics export is not a valid GLB")
    magic, version, total_length = struct.unpack_from("<4sII", data)
    chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
    if version != 2 or total_length != len(data) or chunk_type != 0x4E4F534A:
        raise RuntimeError("physics GLB header is invalid")
    return json.loads(data[20 : 20 + chunk_length].decode("utf-8"))


def _make_hinge_scene(blender: str, scene_path: Path, work: Path) -> None:
    script = work / "make_hinge_scene.py"
    script.write_text(
        """import bpy
import math
from mathutils import Quaternion, Vector
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
scene.render.fps = 24
scene.gravity = (0, 0, -9.81)
bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0, 0, 2))
anchor = bpy.context.object
anchor.name = 'hinge-smoke-anchor'
angle = math.radians(10)
rotation = Quaternion((0, 1, 0), angle)
center = Vector((0, 0, 2)) + rotation @ Vector((0, 0, -1))
bpy.ops.mesh.primitive_cube_add(size=1, location=center)
body = bpy.context.object
body.name = 'hinge-smoke-link'
body.dimensions = (0.15, 0.15, 2.0)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
body.rotation_mode = 'QUATERNION'
body.rotation_quaternion = rotation
bpy.ops.wm.save_as_mainfile(filepath=SCENE_PATH)
""".replace("SCENE_PATH", repr(str(scene_path))),
        encoding="utf-8",
    )
    result = subprocess.run(
        [blender, "--background", "--factory-startup", "--python", str(script)],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode or not scene_path.is_file():
        raise RuntimeError(
            "Blender could not prepare the hinge smoke scene: "
            f"exit={result.returncode}; {result.stderr[-1200:]} {result.stdout[-1200:]}"
        )


def _run_hinge_smoke(blender: str, root: Path) -> dict:
    scene_path = root / "hinge.blend"
    _make_hinge_scene(blender, scene_path, root)
    spec = BlenderPhysicsSpec(
        scene_path=scene_path,
        output_dir=root / "hinge-output",
        moving_object="hinge-smoke-link",
        maneuver="hinge",
        fixed_object="hinge-smoke-anchor",
        hinge_pivot_m=(0.0, 0.0, 2.0),
        hinge_axis=(0.0, 1.0, 0.0),
        hinge_lower_limit_deg=-15.0,
        hinge_upper_limit_deg=15.0,
        fps=24,
        duration_s=3.0,
        solver_substeps=16,
        solver_iterations=50,
        timeout_s=120,
    )
    result = asyncio.run(run_blender_physics_spec(spec, blender_bin=blender))
    if not result.ok or result.simulated or not result.payload:
        raise RuntimeError(
            "Blender physics adapter hinge smoke failed: "
            f"exit={result.exit_code}; error={result.error}; log={result.log_tail}"
        )
    trace = result.payload
    glb_path = result.scene_output_path
    if glb_path is None:
        raise RuntimeError("hinge adapter did not return an animated GLB")
    glb = _glb_json(glb_path)
    joint = trace.get("joint") or {}
    rows = trace.get("frames") or []
    angles = [row["joint_angle_deg"] for row in rows]
    upper = joint.get("upper_limit_deg", 0.0)
    lower = joint.get("lower_limit_deg", 0.0)
    max_angle = max((abs(value) for value in angles), default=0.0)
    if len(rows) < 70 or max_angle < 2.0 or max_angle > max(abs(lower), abs(upper)) + 1.0:
        raise RuntimeError(f"hinge motion or limits invalid: {max_angle} deg in {len(rows)} frames")
    if min(angles) < lower - 1.0 or max(angles) > upper + 1.0:
        raise RuntimeError(f"hinge angle escaped limits: [{min(angles)}, {max(angles)}]")
    if trace.get("reset_pose_delta_m") is None or trace["reset_pose_delta_m"] > 1e-5:
        raise RuntimeError(f"hinge reset pose drifted: {trace.get('reset_pose_delta_m')}")
    if trace.get("reset_rotation_delta_deg") is None or trace["reset_rotation_delta_deg"] > 1e-4:
        raise RuntimeError(
            f"hinge reset rotation drifted: {trace.get('reset_rotation_delta_deg')}"
        )
    if joint.get("type") != "HINGE" or not glb.get("animations"):
        raise RuntimeError("hinge adapter did not preserve joint metadata and animation")
    return {
        "joint": joint["type"],
        "frames": len(rows),
        "limit_deg": [lower, upper],
        "max_angle_deg": trace["max_hinge_angle_deg"],
        "reset_pose_delta_m": trace["reset_pose_delta_m"],
        "reset_rotation_delta_deg": trace["reset_rotation_delta_deg"],
        "state_hash": trace["state_hash"],
        "glb_animations": len(glb["animations"]),
    }

def main() -> int:
    blender = os.environ.get("STUDIO_BLENDER_BIN", "/opt/blender/blender")
    os.environ["STUDIO_BLENDER_BIN"] = blender
    os.environ.pop("STUDIO_STUB_BLENDER", None)
    version = subprocess.run(
        [blender, "--version"], capture_output=True, text=True, timeout=30, check=True
    ).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="studio-blender-physics-") as temporary:
        root = Path(temporary)
        scene_path = root / "physics.blend"
        _make_scene(blender, scene_path, root)
        spec = BlenderPhysicsSpec(
            scene_path=scene_path,
            output_dir=root / "physics-output",
            moving_object="physics-smoke-body",
            ground_object="physics-smoke-ground",
            fps=24,
            duration_s=2,
            solver_substeps=8,
            solver_iterations=20,
            timeout_s=120,
        )
        result = asyncio.run(run_blender_physics_spec(spec, blender_bin=blender))
        if not result.ok or result.simulated or not result.payload:
            raise RuntimeError(
                "Blender physics run did not produce real evidence: "
                f"exit={result.exit_code}; error={result.error}; log={result.log_tail[-1200:]}"
            )
        payload = result.payload
        frames = payload["frames"]
        heights = [row["position_m"][2] for row in frames]
        if len(frames) < 40 or max(heights) - min(heights) < 0.25:
            raise RuntimeError("Bullet trajectory did not show bounded free-fall motion")
        if not payload["contacts"]:
            raise RuntimeError("Bullet trajectory did not report a ground proximity event")
        if len(payload["state_hash"]) != 64 or result.result_path is None:
            raise RuntimeError("physics trace is missing its state hash or JSON artifact")
        glb = _glb_json(result.scene_output_path)
        if not glb.get("animations"):
            raise RuntimeError("physics GLB does not contain the exported trajectory animation")
        hinge = _run_hinge_smoke(blender, root)
        print(
            json.dumps(
                {
                    "engine": "blender-bullet",
                    "version": version,
                    "stubbed": False,
                    "frames": len(frames),
                    "contacts": len(payload["contacts"]),
                    "state_hash": payload["state_hash"],
                    "glb_animations": len(glb["animations"]),
                    "hinge": hinge,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
