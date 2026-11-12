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


def _run_hinge_smoke(blender: str, root: Path) -> dict:
    trace_path = root / "hinge-trace.json"
    glb_path = root / "hinge-scene.glb"
    script_path = root / "hinge-smoke.py"
    script_path.write_text(
        """import bpy
import hashlib
import json
import math
from mathutils import Quaternion, Vector

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.unit_settings.system = 'METRIC'
scene.render.fps = 24
scene.frame_start = 1
scene.frame_end = 73
scene.gravity = (0, 0, -9.81)
bpy.ops.rigidbody.world_add()
world = scene.rigidbody_world
world.substeps_per_frame = 16
world.solver_iterations = 50

bpy.ops.mesh.primitive_cube_add(size=0.2, location=(0, 0, 2))
anchor = bpy.context.object
anchor.name = 'hinge-smoke-anchor'
bpy.ops.rigidbody.object_add()
anchor.rigid_body.type = 'PASSIVE'

initial_angle = math.radians(10)
length = 2.0
initial_rotation = Quaternion((0, 1, 0), initial_angle)
center = Vector((0, 0, 2)) + initial_rotation @ Vector((0, 0, -length / 2))
bpy.ops.mesh.primitive_cube_add(size=1, location=center)
body = bpy.context.object
body.name = 'hinge-smoke-link'
body.dimensions = (0.15, 0.15, length)
bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
body.rotation_mode = 'QUATERNION'
body.rotation_quaternion = initial_rotation
bpy.ops.rigidbody.object_add()
body.rigid_body.type = 'ACTIVE'
body.rigid_body.mass = 1.0

joint = bpy.data.objects.new('hinge-smoke-joint', None)
scene.collection.objects.link(joint)
joint.location = (0, 0, 2)
joint.rotation_euler = (math.pi / 2, 0, 0)
bpy.context.view_layer.objects.active = joint
joint.select_set(True)
bpy.ops.rigidbody.constraint_add(type='HINGE')
constraint = joint.rigid_body_constraint
constraint.object1 = anchor
constraint.object2 = body
constraint.enabled = True
constraint.disable_collisions = True
constraint.use_limit_ang_z = True
constraint.limit_ang_z_lower = math.radians(-15)
constraint.limit_ang_z_upper = math.radians(15)
constraint.solver_iterations = 50

rows = []
previous_position = None
max_angle_deg = 0.0
for frame in range(scene.frame_start, scene.frame_end + 1):
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world.copy()
    position = matrix.translation.copy()
    rotation = matrix.to_quaternion().normalized()
    relative = (rotation @ initial_rotation.inverted()).normalized()
    _axis, angle = relative.to_axis_angle()
    angle_deg = math.degrees(angle)
    max_angle_deg = max(max_angle_deg, angle_deg)
    velocity = Vector((0, 0, 0)) if previous_position is None else (
        (position - previous_position) * scene.render.fps
    )
    rows.append({
        'frame': frame,
        'time_s': (frame - 1) / scene.render.fps,
        'position_m': [float(v) for v in position],
        'linear_velocity_mps': [float(v) for v in velocity],
        'rotation_quaternion_wxyz': [
            float(rotation.w), float(rotation.x), float(rotation.y), float(rotation.z)
        ],
        'relative_hinge_angle_deg': float(angle_deg),
    })
    previous_position = position.copy()

scene.frame_set(1)
bpy.context.view_layer.update()
replayed = body.evaluated_get(bpy.context.evaluated_depsgraph_get()).matrix_world.translation
initial_position = Vector(rows[0]['position_m'])
reset_delta_m = float((replayed - initial_position).length)

body.rigid_body.kinematic = True
body.animation_data_clear()
body.rotation_mode = 'QUATERNION'
for row in rows:
    scene.frame_set(row['frame'])
    body.location = row['position_m']
    body.rotation_quaternion = row['rotation_quaternion_wxyz']
    body.keyframe_insert(data_path='location', frame=row['frame'], group='Hinge trajectory')
    body.keyframe_insert(
        data_path='rotation_quaternion', frame=row['frame'], group='Hinge trajectory'
    )

core = {
    'schema': 'comfy_studio.blender-hinge-smoke.v1',
    'solver': 'Blender Bullet rigid-body world',
    'fps': scene.render.fps,
    'units': {'length': 'm', 'time': 's', 'angle': 'deg'},
    'joint': {
        'type': constraint.type,
        'axis_local': 'Z',
        'lower_limit_rad': constraint.limit_ang_z_lower,
        'upper_limit_rad': constraint.limit_ang_z_upper,
        'lower_limit_deg': math.degrees(constraint.limit_ang_z_lower),
        'upper_limit_deg': math.degrees(constraint.limit_ang_z_upper),
        'anchor_object': anchor.name,
        'moving_object': body.name,
    },
    'initial_pose_m': rows[0]['position_m'],
    'reset_pose_delta_m': reset_delta_m,
    'max_relative_angle_deg': max_angle_deg,
    'frames': rows,
}
canonical = json.dumps(core, sort_keys=True, separators=(',', ':'), allow_nan=False)
core['state_hash'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
bpy.ops.export_scene.gltf(
    filepath=GLB_PATH, export_format='GLB', use_selection=False, export_animations=True
)
with open(TRACE_PATH, 'w', encoding='utf-8') as stream:
    json.dump(core, stream, sort_keys=True, allow_nan=False)
    stream.write('\\n')
""".replace("TRACE_PATH", repr(str(trace_path))).replace("GLB_PATH", repr(str(glb_path))),
        encoding="utf-8",
    )
    result = subprocess.run(
        [blender, "--background", "--factory-startup", "--python", str(script_path)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            "Blender hinge constraint smoke failed: "
            f"exit={result.returncode}; {result.stderr[-1200:]} {result.stdout[-1200:]}"
        )
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    glb = _glb_json(glb_path)
    lower = trace["joint"]["lower_limit_deg"]
    upper = trace["joint"]["upper_limit_deg"]
    angles = [row["relative_hinge_angle_deg"] for row in trace["frames"]]
    if len(angles) < 70 or max(angles) < 1.0 or max(angles) > upper + 1.0:
        raise RuntimeError(f"hinge motion did not respect upper limit: {max(angles)} deg")
    if min(angles) < lower - 1.0 or trace["reset_pose_delta_m"] > 1e-5:
        raise RuntimeError(
            "hinge lower limit or initial-state reset check failed: "
            f"min={min(angles)}, reset_delta={trace['reset_pose_delta_m']}"
        )
    if not glb.get("animations"):
        raise RuntimeError("hinge GLB does not contain its exported animation")
    return {
        "joint": trace["joint"]["type"],
        "frames": len(angles),
        "limit_deg": [lower, upper],
        "max_angle_deg": trace["max_relative_angle_deg"],
        "reset_pose_delta_m": trace["reset_pose_delta_m"],
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
