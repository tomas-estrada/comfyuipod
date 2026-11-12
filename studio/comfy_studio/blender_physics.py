"""Headless CPU rigid-body simulation through the pinned Blender runtime.

The exported trajectory is sampled from Blender's evaluated rigid-body scene.
Contact records are derived from nearest evaluated mesh distance because
Blender's public Python API does not expose Bullet contact manifolds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from comfy_studio.config import SecretRedactor

_GLB_MAGIC = b"glTF"
_MAX_FRAMES = 600
_ALLOWED_SCENES = {".blend", ".glb", ".gltf"}
_SUPPORTED_MANEUVERS = {"free_fall", "hinge"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _physics_state_hash(payload: dict[str, Any]) -> str:
    """Recompute the trace digest from the canonical payload fields."""
    core = {key: value for key, value in payload.items() if key != "state_hash"}
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BlenderPhysicsError(ValueError):
    """Invalid or unsuccessful Blender physics run."""


@dataclass(slots=True)
class BlenderPhysicsSpec:
    """Bounded simulation request against objects in a project-owned scene."""

    scene_path: Path
    output_dir: Path
    moving_object: str
    maneuver: str = "free_fall"
    ground_object: str | None = None
    fixed_object: str | None = None
    hinge_pivot_m: tuple[float, float, float] | None = None
    hinge_axis: tuple[float, float, float] = (0.0, 1.0, 0.0)
    hinge_lower_limit_deg: float = -45.0
    hinge_upper_limit_deg: float = 45.0
    fps: int = 24
    duration_s: float = 2.0
    seed: int = 0
    mass_kg: float = 1.0
    gravity_mps2: float = 9.81
    solver_substeps: int = 8
    solver_iterations: int = 20
    contact_tolerance_m: float = 0.03
    timeout_s: float = 180.0

    def validate(self) -> None:
        if self.scene_path.suffix.lower() not in _ALLOWED_SCENES:
            raise BlenderPhysicsError("scene_path debe terminar en .blend, .glb o .gltf")
        if not self.scene_path.is_file():
            raise BlenderPhysicsError("scene_path no existe o no es un archivo")
        if not self.moving_object.strip():
            raise BlenderPhysicsError("moving_object no puede estar vacío")
        if self.maneuver not in _SUPPORTED_MANEUVERS:
            raise BlenderPhysicsError(
                f"maneuver debe estar en {sorted(_SUPPORTED_MANEUVERS)}"
            )
        if self.maneuver == "free_fall" and not self.ground_object:
            raise BlenderPhysicsError("free_fall requiere ground_object")
        if self.maneuver == "free_fall" and self.ground_object == self.moving_object:
            raise BlenderPhysicsError("moving_object y ground_object deben ser distintos")
        if self.maneuver == "hinge":
            if not self.fixed_object or self.hinge_pivot_m is None:
                raise BlenderPhysicsError(
                    "hinge requiere fixed_object y hinge_pivot_m"
                )
            if self.fixed_object == self.moving_object:
                raise BlenderPhysicsError("moving_object y fixed_object deben ser distintos")
            if (
                len(self.hinge_pivot_m) != 3
                or not all(math.isfinite(value) for value in self.hinge_pivot_m)
                or len(self.hinge_axis) != 3
                or not all(math.isfinite(value) for value in self.hinge_axis)
                or math.isclose(math.sqrt(sum(value**2 for value in self.hinge_axis)), 0.0)
            ):
                raise BlenderPhysicsError(
                    "hinge_pivot_m y hinge_axis deben ser vectores 3D finitos"
                )
            if (
                not math.isfinite(self.hinge_lower_limit_deg)
                or not math.isfinite(self.hinge_upper_limit_deg)
                or not -180 <= self.hinge_lower_limit_deg < self.hinge_upper_limit_deg <= 180
            ):
                raise BlenderPhysicsError(
                    "límites del hinge deben estar ordenados entre -180 y 180 grados"
                )
        if not 1 <= self.fps <= 60:
            raise BlenderPhysicsError("fps debe estar entre 1 y 60")
        if not math.isfinite(self.duration_s) or not 0 < self.duration_s <= 10:
            raise BlenderPhysicsError("duration_s debe estar entre 0 y 10 segundos")
        if round(self.fps * self.duration_s) > _MAX_FRAMES:
            raise BlenderPhysicsError(f"la simulación no puede superar {_MAX_FRAMES} frames")
        if not math.isfinite(self.mass_kg) or not 0.001 <= self.mass_kg <= 1_000_000:
            raise BlenderPhysicsError("mass_kg debe estar entre 0.001 y 1000000")
        if not math.isfinite(self.gravity_mps2) or not 0 <= self.gravity_mps2 <= 100:
            raise BlenderPhysicsError("gravity_mps2 debe estar entre 0 y 100")
        if not 1 <= self.solver_substeps <= 64 or not 1 <= self.solver_iterations <= 128:
            raise BlenderPhysicsError("parámetros del solver fuera de rango")
        if not math.isfinite(self.contact_tolerance_m) or not 0 < self.contact_tolerance_m <= 0.25:
            raise BlenderPhysicsError("contact_tolerance_m debe estar entre 0 y 0.25")
        if not math.isfinite(self.timeout_s) or not 1 <= self.timeout_s <= 900:
            raise BlenderPhysicsError("timeout_s debe estar entre 1 y 900")

    def canonical(self) -> dict[str, Any]:
        value = asdict(self)
        value["scene_path"] = str(self.scene_path.resolve())
        value["output_dir"] = str(self.output_dir.resolve())
        value["source_sha256"] = _sha256_file(self.scene_path)
        value.pop("timeout_s", None)
        return value


@dataclass(slots=True)
class BlenderPhysicsResult:
    """Validated physics artifacts and execution diagnostics."""

    ok: bool
    exit_code: int
    result_path: Path | None = None
    scene_output_path: Path | None = None
    payload: dict[str, Any] | None = None
    log_tail: str = ""
    duration_s: float = 0.0
    error: str | None = None
    simulated: bool = False


def build_physics_script(spec: BlenderPhysicsSpec, result_path: Path, glb_path: Path) -> str:
    """Create the isolated Blender Python entry point for one physics run."""
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    config_path = spec.output_dir / "physics-spec.json"
    config_path.write_text(
        json.dumps(spec.canonical(), ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    config_literal = json.dumps(str(config_path.resolve()).replace("\\", "/"))
    result_literal = json.dumps(str(result_path.resolve()).replace("\\", "/"))
    glb_literal = json.dumps(str(glb_path.resolve()).replace("\\", "/"))
    return f'''# Generated by Comfy Studio. Blender owns the Bullet rigid-body solve.
import bpy
import hashlib
import json
import math
import random
from mathutils import Quaternion, Vector
from mathutils.bvhtree import BVHTree

CONFIG_PATH = {config_literal}
RESULT_PATH = {result_literal}
GLB_PATH = {glb_literal}
with open(CONFIG_PATH, encoding="utf-8") as stream:
    cfg = json.load(stream)

scene_path = cfg["scene_path"]
suffix = scene_path.lower()
if suffix.endswith((".glb", ".gltf")):
    bpy.ops.import_scene.gltf(filepath=scene_path)
elif suffix.endswith(".blend"):
    bpy.ops.wm.open_mainfile(filepath=scene_path)
else:
    raise RuntimeError("unsupported scene extension")

random.seed(int(cfg["seed"]))
scene = bpy.context.scene
scene.unit_settings.system = "METRIC"
unit_scale = float(scene.unit_settings.scale_length or 1.0)
scene.unit_settings.scale_length = unit_scale
scene.render.fps = int(cfg["fps"])
scene.render.fps_base = 1.0
frame_count = max(2, int(round(int(cfg["fps"]) * float(cfg["duration_s"]))))
scene.frame_start = 1
scene.frame_end = frame_count
scene.gravity = (0.0, 0.0, -float(cfg["gravity_mps2"]) / unit_scale)
if scene.rigidbody_world is None:
    bpy.ops.rigidbody.world_add()
world = scene.rigidbody_world
world.enabled = True
world.substeps_per_frame = int(cfg["solver_substeps"])
world.solver_iterations = int(cfg["solver_iterations"])
world.time_scale = 1.0
world.use_split_impulse = True

def set_active(obj):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj

def ensure_rigidbody(obj, body_type):
    if obj.type != "MESH":
        raise RuntimeError("rigid-body participants must be mesh objects")
    if obj.rigid_body is None:
        set_active(obj)
        bpy.ops.rigidbody.object_add()
    obj.rigid_body.type = body_type
    obj.rigid_body.collision_shape = "CONVEX_HULL"
    obj.rigid_body.use_deactivation = False
    obj.rigid_body.friction = 0.6
    obj.rigid_body.restitution = 0.0
    return obj.rigid_body

body = bpy.data.objects.get(cfg["moving_object"])
if body is None:
    raise RuntimeError("moving object not found: " + cfg["moving_object"])
ensure_rigidbody(body, "ACTIVE")
body.rigid_body.mass = float(cfg["mass_kg"])
ground = None
ground_tree = None
ground_inverse = None
ground_eval = None
joint_info = None
if cfg["maneuver"] == "free_fall":
    ground = bpy.data.objects.get(cfg["ground_object"])
    if ground is None:
        raise RuntimeError("ground object not found: " + cfg["ground_object"])
    ensure_rigidbody(ground, "PASSIVE")
elif cfg["maneuver"] == "hinge":
    fixed = bpy.data.objects.get(cfg["fixed_object"])
    if fixed is None:
        raise RuntimeError("fixed object not found: " + cfg["fixed_object"])
    ensure_rigidbody(fixed, "PASSIVE")
    axis = Vector(cfg["hinge_axis"]).normalized()
    joint = bpy.data.objects.new(cfg.get("joint_name") or "comfyuipod_hinge", None)
    scene.collection.objects.link(joint)
    joint.location = Vector(cfg["hinge_pivot_m"]) / unit_scale
    joint.rotation_mode = "QUATERNION"
    joint.rotation_quaternion = Vector((0, 0, 1)).rotation_difference(axis)
    set_active(joint)
    bpy.ops.rigidbody.constraint_add(type="HINGE")
    constraint = joint.rigid_body_constraint
    constraint.object1 = fixed
    constraint.object2 = body
    constraint.enabled = True
    constraint.disable_collisions = True
    constraint.use_limit_ang_z = True
    constraint.limit_ang_z_lower = math.radians(float(cfg["hinge_lower_limit_deg"]))
    constraint.limit_ang_z_upper = math.radians(float(cfg["hinge_upper_limit_deg"]))
    constraint.solver_iterations = int(cfg["solver_iterations"])
    joint_info = {{
        "name": joint.name,
        "type": "HINGE",
        "fixed_object": fixed.name,
        "moving_object": body.name,
        "pivot_m": [float(value) for value in cfg["hinge_pivot_m"]],
        "axis_world": [float(value) for value in axis],
        "axis_local": "Z",
        "lower_limit_deg": float(cfg["hinge_lower_limit_deg"]),
        "upper_limit_deg": float(cfg["hinge_upper_limit_deg"]),
        "lower_limit_rad": float(constraint.limit_ang_z_lower),
        "upper_limit_rad": float(constraint.limit_ang_z_upper),
    }}
    body["comfyuipod_joint"] = json.dumps(joint_info, sort_keys=True)

scene.frame_set(1)
bpy.context.view_layer.update()
depsgraph = bpy.context.evaluated_depsgraph_get()
initial_q = body.evaluated_get(depsgraph).matrix_world.to_quaternion().copy()
initial_position = body.evaluated_get(depsgraph).matrix_world.translation.copy()
if ground is not None:
    ground_eval = ground.evaluated_get(depsgraph)
    ground_tree = BVHTree.FromObject(ground_eval, depsgraph, epsilon=0.0)
    ground_inverse = ground_eval.matrix_world.inverted()

frames = []
contacts = []
previous_position = None
previous_rotation = None
was_in_contact = False
for frame in range(1, frame_count + 1):
    scene.frame_set(frame)
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    evaluated = body.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world.copy()
    position = matrix.translation.copy()
    rotation = matrix.to_quaternion().normalized()
    time_s = (frame - 1) / float(cfg["fps"])
    if previous_position is None:
        linear_velocity = Vector((0,0,0))
        angular_velocity = Vector((0,0,0))
    else:
        dt = 1.0 / float(cfg["fps"])
        linear_velocity = (position - previous_position) * (unit_scale / dt)
        delta = (rotation @ previous_rotation.inverted()).normalized()
        delta_axis, delta_angle = delta.to_axis_angle()
        angular_velocity = delta_axis * (delta_angle / dt)
    nearest = None
    if ground_tree is not None:
        best = None
        for corner in evaluated.bound_box:
            world_corner = matrix @ Vector(corner)
            local_corner = ground_inverse @ world_corner
            hit = ground_tree.find_nearest(local_corner)
            if hit and hit[0] is not None and (best is None or hit[3] < best[3]):
                best = hit
                best_world_point = ground_eval.matrix_world @ hit[0]
                best_world_normal = (ground_eval.matrix_world.to_3x3() @ hit[1]).normalized()
        if best is not None:
            nearest = {{
                "distance_m": float(best[3]) * unit_scale,
                "point_m": [float(x) * unit_scale for x in best_world_point],
                "normal": [float(x) for x in best_world_normal],
            }}
    in_contact = bool(
        nearest is not None and nearest["distance_m"] <= float(cfg["contact_tolerance_m"])
    )
    contact_event = None
    if in_contact and not was_in_contact:
        contact_event = {{
            "frame": frame,
            "time_s": time_s,
            "object": cfg.get("ground_object"),
            **nearest,
            "detection": "evaluated_mesh_proximity_after_bullet_step",
        }}
        contacts.append(contact_event)
    row = {{
        "frame": frame,
        "time_s": time_s,
        "position_m": [float(x) * unit_scale for x in position],
        "rotation_quaternion_wxyz": [
            float(rotation.w),
            float(rotation.x),
            float(rotation.y),
            float(rotation.z),
        ],
        "linear_velocity_mps": [float(x) for x in linear_velocity],
        "angular_velocity_rad_s": [float(x) for x in angular_velocity],
        "in_contact": in_contact,
        "nearest_contact": nearest,
    }}
    if joint_info is not None:
        relative = (rotation @ initial_q.inverted()).normalized()
        signed_projection = (
            relative.x * joint_info["axis_world"][0]
            + relative.y * joint_info["axis_world"][1]
            + relative.z * joint_info["axis_world"][2]
        )
        angle_deg = math.degrees(2.0 * math.atan2(signed_projection, relative.w))
        while angle_deg > 180.0:
            angle_deg -= 360.0
        while angle_deg < -180.0:
            angle_deg += 360.0
        row["joint_angle_deg"] = angle_deg
        row["joint_limit_ok"] = (
            joint_info["lower_limit_deg"] - 1.0
            <= angle_deg
            <= joint_info["upper_limit_deg"] + 1.0
        )
    frames.append(row)
    previous_position = position.copy()
    previous_rotation = rotation.copy()
    was_in_contact = in_contact

if joint_info is not None and not all(row["joint_limit_ok"] for row in frames):
    raise RuntimeError("hinge trajectory exceeded configured angular limits")

reset_pose_delta_m = None
reset_rotation_delta_deg = None
if joint_info is not None:
    scene.frame_set(1)
    bpy.context.view_layer.update()
    replayed = body.evaluated_get(bpy.context.evaluated_depsgraph_get())
    reset_pose_delta_m = float(
        (replayed.matrix_world.translation - initial_position).length * unit_scale
    )
    replayed_q = replayed.matrix_world.to_quaternion().normalized()
    reset_rotation_delta_deg = math.degrees(replayed_q.rotation_difference(initial_q).angle)
    if reset_pose_delta_m > 1e-5 or reset_rotation_delta_deg > 1e-4:
        raise RuntimeError("hinge reset did not restore its initial pose")

finite_channels = (
    row["position_m"] + row["linear_velocity_mps"] + row["angular_velocity_rad_s"]
    for row in frames
)
if not frames or not all(math.isfinite(value) for channel in finite_channels for value in channel):
    raise RuntimeError("simulation produced empty or non-finite frame data")

# Convert the solver trajectory into ordinary editable animation curves.
body.rigid_body.kinematic = True
body.animation_data_clear()
body.rotation_mode = "QUATERNION"
for row in frames:
    scene.frame_set(row["frame"])
    body.location = [value / unit_scale for value in row["position_m"]]
    w, x, y, z = row["rotation_quaternion_wxyz"]
    body.rotation_quaternion = Quaternion((w, x, y, z))
    body.keyframe_insert(data_path="location", frame=row["frame"], group="Rigid-body trajectory")
    body.keyframe_insert(
        data_path="rotation_quaternion",
        frame=row["frame"],
        group="Rigid-body trajectory",
    )

core = {{
    "schema": "comfy_studio.blender-physics.v1",
    "maneuver": cfg["maneuver"],
    "seed": int(cfg["seed"]),
    "blender_version": bpy.app.version_string,
    "fps": int(cfg["fps"]),
    "duration_s": float(cfg["duration_s"]),
    "frame_count": len(frames),
    "units": {{"length": "m", "mass": "kg", "time": "s", "angle": "deg"}},
    "solver": {{
        "name": "Blender Bullet rigid-body world",
        "substeps_per_frame": int(cfg["solver_substeps"]),
        "solver_iterations": int(cfg["solver_iterations"]),
        "gravity_mps2": float(cfg["gravity_mps2"]),
    }},
    "moving_object": cfg["moving_object"],
    "source_sha256": cfg["source_sha256"],
    "ground_object": cfg.get("ground_object"),
    "fixed_object": cfg.get("fixed_object"),
    "joint": joint_info,
    "initial_pose_m": [float(value) * unit_scale for value in initial_position],
    "reset_pose_delta_m": reset_pose_delta_m,
    "reset_rotation_delta_deg": reset_rotation_delta_deg,
    "max_hinge_angle_deg": (
        max(abs(row["joint_angle_deg"]) for row in frames)
        if joint_info is not None
        else None
    ),
    "contact_method": "evaluated_mesh_proximity_after_bullet_step",
    "contact_tolerance_m": float(cfg["contact_tolerance_m"]),
    "contacts": contacts,
    "frames": frames,
}}
canonical = json.dumps(core, sort_keys=True, separators=(",", ":"), allow_nan=False)
state_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
core["state_hash"] = state_hash
body["comfyuipod_physics_state_hash"] = state_hash
body["comfyuipod_physics_seed"] = int(cfg["seed"])
body["comfyuipod_physics_solver"] = "Blender Bullet rigid-body world"
scene["comfyuipod_physics_state_hash"] = state_hash
scene["comfyuipod_physics_seed"] = int(cfg["seed"])
bpy.ops.export_scene.gltf(
    filepath=GLB_PATH,
    export_format="GLB",
    use_selection=False,
    export_animations=True,
    export_extras=True,
)
with open(RESULT_PATH, "w", encoding="utf-8") as stream:
    json.dump(core, stream, ensure_ascii=False, sort_keys=True, allow_nan=False)
    stream.write("\\n")
print(
    "[ComfyUIPodPhysics] state_hash="
    + state_hash
    + " frames="
    + str(len(frames))
    + " contacts="
    + str(len(contacts))
)
'''


async def run_blender_physics_spec(
    spec: BlenderPhysicsSpec,
    *,
    blender_bin: str | None = None,
    cancel_event: Any | None = None,
) -> BlenderPhysicsResult:
    """Run Blender in a separate process and validate the JSON/GLB artifacts."""
    spec.validate()
    started = time.monotonic()
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    binary = blender_bin or os.environ.get("STUDIO_BLENDER_BIN") or shutil.which("blender")
    if not binary:
        return BlenderPhysicsResult(
            ok=False, exit_code=127, error="binario de Blender ausente", duration_s=0.0
        )
    result_path = spec.output_dir / "physics-result.json"
    glb_path = spec.output_dir / "physics-scene.glb"
    script_path = spec.output_dir / "blender_physics_job.py"
    result_path.unlink(missing_ok=True)
    glb_path.unlink(missing_ok=True)
    script_path.write_text(build_physics_script(spec, result_path, glb_path), encoding="utf-8")
    redactor = SecretRedactor.from_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-b",
            "--python",
            str(script_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(spec.output_dir),
        )
    except OSError as exc:
        return BlenderPhysicsResult(
            ok=False,
            exit_code=126,
            duration_s=time.monotonic() - started,
            error=f"no se pudo lanzar Blender: {exc}",
        )
    try:
        communicate = asyncio.create_task(proc.communicate())
        deadline = time.monotonic() + spec.timeout_s
        while True:
            if cancel_event is not None and (
                cancel_event.is_set()
                if hasattr(cancel_event, "is_set")
                else bool(cancel_event() if callable(cancel_event) else cancel_event)
            ):
                proc.kill()
                stdout, _ = await communicate
                text = redactor.redact((stdout or b"").decode("utf-8", errors="replace"))
                return BlenderPhysicsResult(
                    ok=False,
                    exit_code=-2,
                    log_tail=text[-4096:],
                    duration_s=time.monotonic() - started,
                    error="simulación cancelada",
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                stdout, _ = await communicate
                text = redactor.redact((stdout or b"").decode("utf-8", errors="replace"))
                return BlenderPhysicsResult(
                    ok=False,
                    exit_code=124,
                    log_tail=text[-4096:],
                    duration_s=time.monotonic() - started,
                    error=f"simulación excedió {spec.timeout_s:.0f}s",
                )
            try:
                stdout, _ = await asyncio.wait_for(
                    asyncio.shield(communicate),
                    timeout=min(0.25, remaining),
                )
                break
            except TimeoutError:
                continue
    except asyncio.CancelledError:
        proc.kill()
        await proc.communicate()
        raise
    raw_log = redactor.redact((stdout or b"").decode("utf-8", errors="replace"))
    duration = time.monotonic() - started
    if proc.returncode != 0:
        return BlenderPhysicsResult(
            ok=False,
            exit_code=int(proc.returncode or 1),
            log_tail=raw_log[-4096:],
            duration_s=duration,
            error=f"Blender physics terminó con código {proc.returncode}",
        )
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        with glb_path.open("rb") as exported_scene:
            glb_header = exported_scene.read(12)
        if len(glb_header) != 12:
            raise ValueError("physics GLB is missing its complete header")
        glb_magic, glb_version, glb_length = struct.unpack("<4sII", glb_header)
        if (
            payload.get("schema") != "comfy_studio.blender-physics.v1"
            or not isinstance(payload.get("frames"), list)
            or not payload["frames"]
            or payload.get("frame_count") != len(payload["frames"])
            or len(payload["state_hash"]) != 64
            or not re.fullmatch(r"[0-9a-f]{64}", payload["state_hash"])
            or payload["state_hash"] != _physics_state_hash(payload)
            or not glb_path.is_file()
            or glb_magic != _GLB_MAGIC
            or glb_version != 2
            or glb_length != glb_path.stat().st_size
        ):
            raise ValueError("physics result failed schema or artifact checks")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return BlenderPhysicsResult(
            ok=False,
            exit_code=int(proc.returncode or 1),
            log_tail=raw_log[-4096:],
            duration_s=duration,
            error=f"resultado de Blender physics inválido: {exc}",
        )
    return BlenderPhysicsResult(
        ok=True,
        exit_code=0,
        result_path=result_path,
        scene_output_path=glb_path,
        payload=payload,
        log_tail=raw_log[-4096:],
        duration_s=duration,
    )


__all__ = [
    "BlenderPhysicsError",
    "BlenderPhysicsResult",
    "BlenderPhysicsSpec",
    "build_physics_script",
    "run_blender_physics_spec",
]
