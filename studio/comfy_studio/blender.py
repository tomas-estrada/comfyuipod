"""BlenderRunner — ejecución headless de Blender 4.x (M3-D, tareas 27-28).

Diseño:
- Blender se invoca SIEMPRE como subproceso CLI independiente (`blender -b ...
  --python <script>`): aislamiento de licencia GPL (sin linking con el código
  del studio, ver docs/decisiones.md) y de fallos (un crash de Blender no tumba
  el servidor).
- Dos modos de trabajo:
  * `render`: importa una escena/malla (GLB/USD/blend), configura Cycles
    (OptiX/CUDA, device seleccionable) o Eevee Next, y renderiza frames a
    PNG/EXR + MP4 vía ffmpeg del pod.
  * `export`: convierte mallas a USD (.usdc), glTF (.glb) y FBX para ingesta
    directa en Unreal Engine (Datasmith) o Blender.
- `STUDIO_STUB_BLENDER=1` simula la ejecución (tests sin binario): escribe los
  outputs declarados con contenido mínimo y un log de stub.
- Logs a `blender.log` en el directorio de datos del studio (rotados por el
  mismo mecanismo que el resto de logs del pod), con redacción de secretos.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from comfy_studio.config import SecretRedactor

logger = logging.getLogger("comfy_studio.blender")

BLENDER_LOG_NAME = "blender.log"
DEFAULT_BLENDER_TIMEOUT_S = 3600.0
LOG_TAIL_BYTES = 4096

_ENGINES = ("cycles", "eevee")
_EXPORT_FORMATS = ("usd", "gltf", "fbx")

# Bytes mínimos válidos-ish para el modo stub (tests): PNG 1x1 y cabeceras.
_STUB_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_STUB_EXR = b"\x76\x2f\x31\x01" + b"\x00" * 32
_STUB_GLB_HEADER = b"glTF\x02\x00\x00\x00\x14\x00\x00\x00" + b"\x00" * 12
_STUB_USD = b'#usda 1.0\n( defaultPrim = "World" )\ndef Xform "World" {}\n'
_STUB_FBX = b"; FBX stub (M3-D STUDIO_STUB_BLENDER)\n"

# Magic bytes reales por extensión para validar artefactos producidos.
_GLTF_MAGIC = b"glTF"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_EXR_MAGIC = b"\x76\x2f\x31\x01"
_FORMAT_MAGICS = {".png": _PNG_MAGIC, ".exr": _EXR_MAGIC, ".glb": _GLTF_MAGIC}

#: clase de fallo cuando el run acaba "bien" pero los artefactos no valen.
FAILURE_INVALID_OUTPUT = "blender_invalid_output"


@dataclass(frozen=True, slots=True)
class BlenderCameraView:
    """A named, bounded still camera pose in the asset's declared coordinates."""

    name: str
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    fov_deg: float = 50.0
    light_rig: dict[str, Any] | None = None

    def validate(self) -> None:
        if not self.name.strip() or len(self.name) > 80:
            raise ValueError("camera view name debe tener entre 1 y 80 caracteres")
        if (
            len(self.position) != 3
            or len(self.target) != 3
            or any(not math.isfinite(value) for value in (*self.position, *self.target))
        ):
            raise ValueError(f"camera view {self.name}: position y target deben ser 3D finitos")
        if all(
            math.isclose(a, b, abs_tol=1e-9)
            for a, b in zip(self.position, self.target, strict=True)
        ):
            raise ValueError(f"camera view {self.name}: position y target no pueden coincidir")
        if not math.isfinite(self.fov_deg) or not 1.0 <= self.fov_deg <= 175.0:
            raise ValueError(f"camera view {self.name}: fov_deg debe estar entre 1 y 175")
        if self.light_rig is not None:
            if set(self.light_rig) != {"key", "fill", "ambient_intensity"}:
                raise ValueError(f"camera view {self.name}: light_rig tiene campos inválidos")
            ambient = self.light_rig["ambient_intensity"]
            if (
                not isinstance(ambient, (int, float))
                or not math.isfinite(ambient)
                or not 0 <= ambient <= 1
            ):
                raise ValueError(f"camera view {self.name}: ambient_intensity fuera de [0, 1]")
            for name in ("key", "fill"):
                light = self.light_rig[name]
                if not isinstance(light, dict) or set(light) != {"position", "intensity", "size"}:
                    raise ValueError(f"camera view {self.name}: light {name} inválida")
                position = light["position"]
                intensity = light["intensity"]
                size = light["size"]
                if (
                    not isinstance(position, (list, tuple))
                    or len(position) != 3
                    or any(
                        not isinstance(value, (int, float)) or not math.isfinite(value)
                        for value in position
                    )
                    or not isinstance(intensity, (int, float))
                    or not math.isfinite(intensity)
                    or not 0 <= intensity <= 4
                    or not isinstance(size, (int, float))
                    or not math.isfinite(size)
                    or not 0 < size <= 100_000
                ):
                    raise ValueError(f"camera view {self.name}: valores del light {name} inválidos")


@dataclass(frozen=True, slots=True)
class BlenderCameraKeyframe:
    """A sampled animation-frame camera pose in the asset's declared coordinates."""

    frame: int
    position: tuple[float, float, float]
    target: tuple[float, float, float]
    fov_deg: float = 50.0

    def validate(self) -> None:
        if isinstance(self.frame, bool) or not isinstance(self.frame, int) or self.frame < 1:
            raise ValueError("camera trajectory frame debe ser un entero positivo")
        if (
            len(self.position) != 3
            or len(self.target) != 3
            or any(
                not isinstance(value, (int, float)) or not math.isfinite(value)
                for value in (*self.position, *self.target)
            )
        ):
            raise ValueError("camera trajectory position and target must be finite 3D values")
        if all(
            math.isclose(a, b, abs_tol=1e-9)
            for a, b in zip(self.position, self.target, strict=True)
        ):
            raise ValueError("camera trajectory position and target cannot coincide")
        if (
            not isinstance(self.fov_deg, (int, float))
            or not math.isfinite(self.fov_deg)
            or not 1.0 <= self.fov_deg <= 175.0
        ):
            raise ValueError("camera trajectory fov_deg must be between 1 and 175")


def find_blender_bin() -> str | None:
    """Localiza el binario de Blender: env STUDIO_BLENDER_BIN, PATH o /opt/blender."""
    env_bin = (os.environ.get("STUDIO_BLENDER_BIN") or "").strip()
    if env_bin:
        return env_bin
    which = shutil.which("blender")
    if which:
        return which
    for candidate in ("/opt/blender/blender", "/usr/local/bin/blender", "/usr/bin/blender"):
        if Path(candidate).is_file():
            return candidate
    return None


def is_stub_mode() -> bool:
    return (os.environ.get("STUDIO_STUB_BLENDER") or "").strip().lower() in ("1", "true", "yes")


@dataclass(slots=True)
class BlenderRenderSpec:
    """Parámetros de un render headless."""

    scene_path: Path
    output_dir: Path
    engine: str = "cycles"
    samples: int = 64
    width: int = 1280
    height: int = 720
    frames: int = 1
    device_index: int = 0
    device: str = "gpu"  # gpu | cpu; CPU is required for deterministic engine fixtures.
    output_format: str = "png"  # png | exr (secuencia) — el MP4 se compone con ffmpeg
    camera_views: tuple[BlenderCameraView, ...] = ()
    camera_trajectory: tuple[BlenderCameraKeyframe, ...] = ()
    asset_transform: tuple[tuple[float, ...], ...] = (
        (1.0, 0.0, 0.0, 0.0),
        (0.0, 1.0, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    up_axis: str = "y_up"
    transparent_background: bool = False
    timeout_s: float = DEFAULT_BLENDER_TIMEOUT_S

    def validate(self) -> None:
        if self.engine not in _ENGINES:
            raise ValueError(f"engine debe ser uno de {_ENGINES}, no {self.engine!r}")
        if self.samples < 1 or self.samples > 4096:
            raise ValueError("samples fuera de rango (1..4096)")
        if self.width < 16 or self.width > 8192 or self.height < 16 or self.height > 8192:
            raise ValueError("width/height fuera de rango (16..8192)")
        if self.frames < 1 or self.frames > 2048:
            raise ValueError("frames fuera de rango (1..2048)")
        if self.output_format not in ("png", "exr"):
            raise ValueError("output_format debe ser 'png' o 'exr'")
        if self.device_index < 0 or self.device_index > 15:
            raise ValueError("device_index fuera de rango (0..15)")
        if self.device not in ("cpu", "gpu"):
            raise ValueError("device debe ser 'cpu' o 'gpu'")
        if len(self.camera_views) > 8:
            raise ValueError("camera_views no puede superar 8 vistas por ejecución")
        names = [view.name for view in self.camera_views]
        if len(names) != len(set(names)):
            raise ValueError("camera_views contiene nombres duplicados")
        for view in self.camera_views:
            view.validate()
        for keyframe in self.camera_trajectory:
            keyframe.validate()
        if self.camera_trajectory:
            frames = [keyframe.frame for keyframe in self.camera_trajectory]
            if any(
                current <= previous
                for previous, current in zip(frames, frames[1:], strict=False)
            ):
                raise ValueError("camera trajectory frames must be strictly increasing")
            if frames[-1] > self.frames:
                raise ValueError("camera_trajectory frame cannot exceed render frames")
            if len(self.camera_trajectory) < 2 or len(self.camera_trajectory) != self.frames:
                raise ValueError(
                    "camera_trajectory requires one camera pose for every render frame"
                )
            if frames[0] != 1 or frames[-1] != self.frames:
                raise ValueError("camera_trajectory must cover frames 1 through render frames")
            if self.camera_views:
                raise ValueError("camera_trajectory cannot be combined with camera_views")
            if (
                self.frames < 2
                or self.output_format != "png"
                or self.engine != "cycles"
                or self.device != "cpu"
            ):
                raise ValueError(
                    "camera_trajectory requires animation PNG with Cycles CPU"
                )
        if self.camera_views and (
            self.frames != 1
            or self.output_format != "png"
            or self.engine != "cycles"
            or self.device != "cpu"
        ):
            raise ValueError("camera_views requiere un frame PNG con Cycles CPU")
        if self.transparent_background and not self.camera_views:
            raise ValueError("transparent_background requiere camera_views")
        if self.up_axis not in ("y_up", "z_up"):
            raise ValueError("up_axis debe ser y_up o z_up")
        if len(self.asset_transform) != 4 or any(len(row) != 4 for row in self.asset_transform):
            raise ValueError("asset_transform debe ser 4x4")
        if any(not math.isfinite(value) for row in self.asset_transform for value in row):
            raise ValueError("asset_transform contiene valores no finitos")
        if any(
            abs(actual - expected) > 1e-9
            for actual, expected in zip(self.asset_transform[3], (0.0, 0.0, 0.0, 1.0), strict=True)
        ):
            raise ValueError("asset_transform debe ser afín con última fila [0, 0, 0, 1]")


@dataclass(slots=True)
class BlenderExportSpec:
    """Parámetros de exportación a formatos UE-ready."""

    scene_path: Path
    output_dir: Path
    formats: tuple[str, ...] = ("usd", "gltf", "fbx")
    timeout_s: float = 600.0

    def validate(self) -> None:
        if not self.formats:
            raise ValueError("formats no puede estar vacío")
        for fmt in self.formats:
            if fmt not in _EXPORT_FORMATS:
                raise ValueError(f"formato de export desconocido: {fmt!r}")


@dataclass(slots=True)
class BlenderResult:
    """Resultado de una ejecución de Blender."""

    exit_code: int
    outputs: list[Path] = field(default_factory=list)
    log_tail: str = ""
    duration_s: float = 0.0
    stubbed: bool = False
    #: motivo de rechazo de artefactos (ausencia/vacíos/magic bytes), None si
    #: todos los artefactos producidos valen. Un run puede acabar con
    #: `exit_code == 0` y aun así NO ser ok: "acabó" no es "produjo artefacto".
    validation_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.validation_error is None


def validate_blender_outputs(
    spec: BlenderRenderSpec | BlenderExportSpec, outputs: list[Path]
) -> str | None:
    """Valida los artefactos de un run. None = válidos; str = motivo de rechazo.

    - vacío: un render/export que no escribió NADA no es un éxito (el modo
      export ya lo protegía a nivel de script; esto lo cubre a nivel de run,
      y aplica también al modo render, que no tenía ningún check).
    - magic bytes: PNG/EXR/GLB con cabecera inválida se rechazan (un `.glb`
      truncado o un PNG corrupto no deben registrarse como artefacto).
    - export glTF reclamado: si se pidió `gltf` y no se produjo ningún `.glb`,
      el paquete UE-ready está incompleto.
    """
    if not outputs:
        if isinstance(spec, BlenderRenderSpec):
            return (
                f"render sin artefactos: Blender terminó sin escribir frames en {spec.output_dir}"
            )
        return f"export sin artefactos: no se escribió ningún fichero en {spec.output_dir}"
    if isinstance(spec, BlenderExportSpec) and "gltf" in spec.formats:
        if not any(p.suffix.lower() == ".glb" for p in outputs):
            return "export glTF reclamado pero no se produjo ningún .glb"
    for p in outputs:
        magic = _FORMAT_MAGICS.get(p.suffix.lower())
        if magic is None:
            continue
        try:
            with p.open("rb") as artifact:
                head = artifact.read(len(magic))
        except OSError as exc:
            return f"artefacto ilegible {p.name}: {exc}"
        if head != magic:
            return f"magic bytes de {p.suffix} inválidos en {p.name}"
    return None


def classify_blender_failure(exit_code: int, log_tail: str) -> str:
    """Clasificación triage-friendly de fallos de Blender (patrones M3-D)."""
    lowered = log_tail.lower()
    if exit_code == 0:
        return "ok"
    if "out of memory" in lowered or "cuda error: out of memory" in lowered:
        return "blender_oom"
    if "optix" in lowered:
        return "blender_optix_error"
    if "no objects" in lowered or "nothing to export" in lowered:
        return "blender_empty_scene"
    if exit_code < 0:
        return "blender_signal_killed"
    return "blender_nonzero_exit"


def _camera_view_render_script(spec: BlenderRenderSpec) -> str:
    """Generate a real CPU scene render for each persisted asset camera pose."""
    # Blender executes this payload as Python source, so JSON's `null`/`true`/
    # `false` tokens are invalid here. Dataclass values are validated above and
    # repr emits safe Python literals for None, booleans, strings, and numbers.
    views = repr([asdict(view) for view in spec.camera_views])
    transform = json.dumps([list(row) for row in spec.asset_transform])
    up_axis = json.dumps(spec.up_axis)
    transparent_background = "True" if spec.transparent_background else "False"
    return f"""\
import math
import re
from mathutils import Matrix, Vector

camera_views = {views}
asset_matrix = Matrix({transform})
asset_up_axis = {up_axis}
transparent_background = {transparent_background}
scene.render.image_settings.file_format = "PNG"
scene.render.image_settings.color_mode = "RGBA" if transparent_background else "RGB"
scene.render.resolution_percentage = 100
scene.view_settings.view_transform = "Standard"
scene.render.film_transparent = transparent_background

# Apply the manifest's global transform to imported asset roots. Child nodes
# retain their local hierarchy and Blender composes their world transforms.
for obj in list(scene.objects):
    if obj.type in {{"MESH", "EMPTY", "ARMATURE"}} and obj.parent is None:
        obj.matrix_world = asset_matrix @ obj.matrix_world

mesh_objects = [obj for obj in scene.objects if obj.type == "MESH"]
if not mesh_objects:
    raise RuntimeError("asset camera preview requires verified mesh geometry")
depsgraph = bpy.context.evaluated_depsgraph_get()
world_corners = [
    obj.evaluated_get(depsgraph).matrix_world @ Vector(corner)
    for obj in mesh_objects
    for corner in obj.evaluated_get(depsgraph).bound_box
]
bounds_min = Vector(tuple(min(point[i] for point in world_corners) for i in range(3)))
bounds_max = Vector(tuple(max(point[i] for point in world_corners) for i in range(3)))
asset_center = (bounds_min + bounds_max) * 0.5
asset_radius = max((bounds_max - bounds_min).length * 0.5, 0.1)

def to_blender_space(values):
    point = Vector(values)
    if asset_up_axis == "y_up":
        point = Vector((point.x, -point.z, point.y))
    return asset_matrix @ point

def aim_at(obj, target):
    obj.rotation_euler = (target - obj.location).to_track_quat("-Z", "Y").to_euler()

if scene.world is None:
    scene.world = bpy.data.worlds.new("Comfy Studio Preview World")
scene.world.use_nodes = True
background = scene.world.node_tree.nodes.get("Background")
if background is not None:
    background.inputs["Color"].default_value = (0.055, 0.065, 0.08, 1.0)
    background.inputs["Strength"].default_value = 0.8

# Add a small, reproducible three-point studio rig only when the imported scene
# does not already provide lights.
if not any(obj.type == "LIGHT" for obj in scene.objects):
    for light_name, direction, energy_scale, size_scale in (
        ("ComfyPreviewKey", (1.4, -2.0, 2.2), 500.0, 2.0),
        ("ComfyPreviewFill", (-2.0, -1.0, 0.8), 220.0, 2.5),
        ("ComfyPreviewRim", (0.4, 2.0, 1.6), 350.0, 1.8),
    ):
        light_data = bpy.data.lights.new(light_name, type="AREA")
        light_data.energy = energy_scale * asset_radius * asset_radius
        light_data.shape = "DISK"
        light_data.size = size_scale * asset_radius
        light_obj = bpy.data.objects.new(light_name, light_data)
        scene.collection.objects.link(light_obj)
        light_obj.location = asset_center + Vector(direction) * (3.0 * asset_radius)
        aim_at(light_obj, asset_center)

for index, view in enumerate(camera_views, start=1):
    rig = view.get("light_rig")
    muted_lights = []
    rig_lights = []
    if rig:
        for existing_light in (obj for obj in scene.objects if obj.type == "LIGHT"):
            muted_lights.append((existing_light, existing_light.hide_render))
            existing_light.hide_render = True
        if background is not None:
            background.inputs["Strength"].default_value = float(rig["ambient_intensity"])
        for light_name in ("key", "fill"):
            config = rig[light_name]
            light_data = bpy.data.lights.new("ComfyStaging_" + light_name, type="AREA")
            light_data.energy = 500.0 * float(config["intensity"]) * asset_radius * asset_radius
            light_data.shape = "DISK"
            light_data.size = float(config["size"])
            light_obj = bpy.data.objects.new(light_data.name, light_data)
            scene.collection.objects.link(light_obj)
            light_obj.location = to_blender_space(config["position"])
            aim_at(light_obj, asset_center)
            rig_lights.append(light_obj)
    camera_data = bpy.data.cameras.new("ComfyPreviewCamera_" + str(index))
    camera_obj = bpy.data.objects.new(camera_data.name, camera_data)
    scene.collection.objects.link(camera_obj)
    camera_obj.location = to_blender_space(view["position"])
    target = to_blender_space(view["target"])
    aim_at(camera_obj, target)
    camera_data.angle = math.radians(float(view["fov_deg"]))
    camera_data.clip_start = max(asset_radius * 0.001, 0.001)
    camera_data.clip_end = max(asset_radius * 1000.0, 100.0)
    scene.camera = camera_obj
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", view["name"]).strip("_")[:60]
    if not slug:
        slug = "view"
    scene.render.filepath = out_dir + "/view_" + str(index).zfill(2) + "_" + slug + ".png"
    bpy.ops.render.render(write_still=True)
    bpy.data.objects.remove(camera_obj, do_unlink=True)
    for light_obj in rig_lights:
        light_data = light_obj.data
        bpy.data.objects.remove(light_obj, do_unlink=True)
        bpy.data.lights.remove(light_data)
    for existing_light, was_hidden in muted_lights:
        existing_light.hide_render = was_hidden
print("[BlenderRunner] camera views rendered:", len(camera_views))
"""


def _camera_trajectory_render_script(spec: BlenderRenderSpec) -> str:
    """Render one sampled, project-coordinate camera pose per integer frame."""
    trajectory = repr([asdict(keyframe) for keyframe in spec.camera_trajectory])
    transform = json.dumps([list(row) for row in spec.asset_transform])
    up_axis = json.dumps(spec.up_axis)
    return f'''\
import math
from mathutils import Matrix, Vector

camera_trajectory = {trajectory}
asset_matrix = Matrix({transform})
asset_up_axis = {up_axis}

for obj in list(scene.objects):
    if obj.type in {{"MESH", "EMPTY", "ARMATURE"}} and obj.parent is None:
        obj.matrix_world = asset_matrix @ obj.matrix_world

mesh_objects = [obj for obj in scene.objects if obj.type == "MESH"]
if not mesh_objects:
    raise RuntimeError("camera trajectory render requires mesh geometry")
depsgraph = bpy.context.evaluated_depsgraph_get()
world_corners = [
    obj.evaluated_get(depsgraph).matrix_world @ Vector(corner)
    for obj in mesh_objects
    for corner in obj.evaluated_get(depsgraph).bound_box
]
bounds_min = Vector(tuple(min(point[i] for point in world_corners) for i in range(3)))
bounds_max = Vector(tuple(max(point[i] for point in world_corners) for i in range(3)))
asset_center = (bounds_min + bounds_max) * 0.5
asset_radius = max((bounds_max - bounds_min).length * 0.5, 0.1)

def to_blender_space(values):
    point = Vector(values)
    if asset_up_axis == "y_up":
        point = Vector((point.x, -point.z, point.y))
    return asset_matrix @ point

camera_data = bpy.data.cameras.new("ComfyCameraTrajectory")
camera_data.sensor_fit = "HORIZONTAL"
camera_data.sensor_width = 36.0
camera_obj = bpy.data.objects.new(camera_data.name, camera_data)
scene.collection.objects.link(camera_obj)
scene.camera = camera_obj
camera_data.clip_start = max(asset_radius * 0.001, 0.001)
camera_data.clip_end = max(asset_radius * 1000.0, 100.0)

if scene.world is None:
    scene.world = bpy.data.worlds.new("ComfyCameraTrajectoryWorld")
scene.world.use_nodes = True
background = scene.world.node_tree.nodes.get("Background")
if background is not None:
    background.inputs["Color"].default_value = (0.055, 0.065, 0.08, 1.0)
    background.inputs["Strength"].default_value = 0.8
if not any(obj.type == "LIGHT" for obj in scene.objects):
    for light_name, direction, energy_scale, size_scale in (
        ("ComfyTrajectoryKey", (1.4, -2.0, 2.2), 500.0, 2.0),
        ("ComfyTrajectoryFill", (-2.0, -1.0, 0.8), 220.0, 2.5),
        ("ComfyTrajectoryRim", (0.4, 2.0, 1.6), 350.0, 1.8),
    ):
        light_data = bpy.data.lights.new(light_name, type="AREA")
        light_data.energy = energy_scale * asset_radius * asset_radius
        light_data.shape = "DISK"
        light_data.size = size_scale * asset_radius
        light_obj = bpy.data.objects.new(light_name, light_data)
        scene.collection.objects.link(light_obj)
        light_obj.location = asset_center + Vector(direction) * (3.0 * asset_radius)
        light_obj.rotation_euler = (
            (asset_center - light_obj.location).to_track_quat("-Z", "Y").to_euler()
        )

for keyframe in camera_trajectory:
    scene.frame_set(keyframe["frame"])
    camera_obj.location = to_blender_space(keyframe["position"])
    target = to_blender_space(keyframe["target"])
    direction = target - camera_obj.location
    if direction.length < 1e-9:
        raise RuntimeError("camera trajectory position and target coincide")
    camera_obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    camera_data.lens = camera_data.sensor_width / (
        2.0 * math.tan(math.radians(float(keyframe["fov_deg"])) * 0.5)
    )
    camera_obj.keyframe_insert(data_path="location", frame=keyframe["frame"])
    camera_obj.keyframe_insert(data_path="rotation_euler", frame=keyframe["frame"])
    camera_data.keyframe_insert(data_path="lens", frame=keyframe["frame"])

scene.frame_set(1)
bpy.ops.render.render(animation=True, write_still=True)
print("[BlenderRunner] camera trajectory frames rendered:", len(camera_trajectory))
'''


def build_render_script(spec: BlenderRenderSpec) -> str:
    """Genera el script Python que Blender ejecuta en modo `-b`."""
    scene_literal = json.dumps(str(spec.scene_path).replace("\\", "/"))
    out_dir_literal = json.dumps(str(spec.output_dir).replace("\\", "/"))
    engine_setup = (
        'scene.render.engine = "CYCLES"'
        if spec.engine == "cycles"
        else """\
available_engines = {
    item.identifier
    for item in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items
}
eevee_engine = next(
    (name for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE") if name in available_engines),
    None,
)
if eevee_engine is None:
    raise RuntimeError("this Blender build does not provide EEVEE")
scene.render.engine = eevee_engine"""
    )
    gpu_setup = ""
    if spec.device == "gpu":
        gpu_setup = """\
    prefs = bpy.context.preferences.addons.get("cycles")
    if prefs is not None:
        cprefs = prefs.preferences
        try:
            cprefs.get_devices_for_type("OPTIX")
            cprefs.compute_device_type = "OPTIX"
        except Exception:
            try:
                cprefs.compute_device_type = "CUDA"
            except Exception:
                cprefs.compute_device_type = "NONE"
        devices = getattr(cprefs, "devices", [])
        target = DEVICE_INDEX
        idx = 0
        for d in devices:
            if getattr(d, "type", "") in ("OPTIX", "CUDA"):
                d.use = (idx == target)
                idx += 1
            else:
                d.use = False""".replace("DEVICE_INDEX", str(spec.device_index))
    render_invocation = (
        _camera_trajectory_render_script(spec)
        if spec.camera_trajectory
        else _camera_view_render_script(spec)
        if spec.camera_views
        else f"bpy.ops.render.render(animation={spec.frames > 1}, write_still=True)"
    )
    return f'''# Generado por Comfy Studio BlenderRunner (M3-D). No editar a mano.
import bpy, sys

scene_path = {scene_literal}
out_dir = {out_dir_literal}

if scene_path.lower().endswith((".glb", ".gltf")):
    bpy.ops.import_scene.gltf(filepath=scene_path)
elif scene_path.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):
    bpy.ops.wm.usd_import(filepath=scene_path)
elif scene_path.lower().endswith(".obj"):
    bpy.ops.wm.obj_import(filepath=scene_path)
else:
    bpy.ops.wm.open_mainfile(filepath=scene_path)

scene = bpy.context.scene
{engine_setup}
scene.render.resolution_x = {spec.width}
scene.render.resolution_y = {spec.height}
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "{"OPEN_EXR" if spec.output_format == "exr" else "PNG"}"
scene.render.filepath = out_dir + "/render_"
scene.frame_start = 1
scene.frame_end = {spec.frames}

if scene.render.engine == "CYCLES":
    scene.cycles.samples = {spec.samples}
    scene.cycles.device = {json.dumps("CPU" if spec.device == "cpu" else "GPU")}
{gpu_setup}
else:
    scene.eevee.taa_render_samples = {spec.samples}

{render_invocation}
print("[BlenderRunner] render completado:", out_dir)
'''


def build_export_script(spec: BlenderExportSpec) -> str:
    """Genera el script Python de exportación USD/glTF/FBX desde una malla/escena."""
    scene_literal = json.dumps(str(spec.scene_path).replace("\\", "/"))
    out_dir_literal = json.dumps(str(spec.output_dir).replace("\\", "/"))
    stem_literal = json.dumps(spec.scene_path.stem)
    lines = [
        "# Generado por Comfy Studio BlenderRunner (M3-D). No editar a mano.",
        "import bpy",
        "",
        f"scene_path = {scene_literal}",
        f"out_dir = {out_dir_literal}",
        f"stem = {stem_literal}",
        "",
        'if scene_path.lower().endswith((".glb", ".gltf")):',
        "    bpy.ops.import_scene.gltf(filepath=scene_path)",
        'elif scene_path.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):',
        "    bpy.ops.wm.usd_import(filepath=scene_path)",
        'elif scene_path.lower().endswith(".obj"):',
        "    bpy.ops.wm.obj_import(filepath=scene_path)",
        "else:",
        "    bpy.ops.wm.open_mainfile(filepath=scene_path)",
        "",
        "if not bpy.data.objects:",
        '    raise RuntimeError("nothing to export: escena sin objetos")',
        "",
    ]
    if "usd" in spec.formats:
        lines += [
            "bpy.ops.wm.usd_export(",
            '    filepath=out_dir + "/" + stem + ".usdc",',
            "    export_materials=True,",
            "    overwrite=True,",
            ")",
        ]
    if "gltf" in spec.formats:
        lines += [
            "bpy.ops.export_scene.gltf(",
            '    filepath=out_dir + "/" + stem + ".glb",',
            '    export_format="GLB",',
            "    use_selection=False,",
            ")",
        ]
    if "fbx" in spec.formats:
        lines += [
            "bpy.ops.export_scene.fbx(",
            '    filepath=out_dir + "/" + stem + ".fbx",',
            "    use_selection=False,",
            ")",
        ]
    lines.append('print("[BlenderRunner] export completado:", out_dir)')
    return "\n".join(lines) + "\n"


def _stub_outputs_for(spec: BlenderRenderSpec | BlenderExportSpec) -> list[Path]:
    """Crea outputs simulados deterministas para STUDIO_STUB_BLENDER=1."""
    created: list[Path] = []
    out_dir = Path(spec.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if isinstance(spec, BlenderRenderSpec):
        count = spec.frames if spec.frames > 1 else 1
        ext = "exr" if spec.output_format == "exr" else "png"
        blob = _STUB_EXR if spec.output_format == "exr" else _STUB_PNG
        for i in range(count):
            suffix = f"_{i + 1:04d}" if spec.frames > 1 else ""
            path = out_dir / f"render{suffix}.{ext}"
            path.write_bytes(blob)
            created.append(path)
    else:
        stem = Path(spec.scene_path).stem
        payloads = {
            "usd": (".usdc", _STUB_USD),
            "gltf": (".glb", _STUB_GLB_HEADER),
            "fbx": (".fbx", _STUB_FBX),
        }
        for fmt in spec.formats:
            ext, blob = payloads[fmt]
            path = out_dir / f"{stem}{ext}"
            path.write_bytes(blob)
            created.append(path)
    return created


async def run_blender_spec(
    spec: BlenderRenderSpec | BlenderExportSpec,
    *,
    log_dir: Path,
    blender_bin: str | None = None,
) -> BlenderResult:
    """Ejecuta Blender headless para un spec de render o export.

    - Stub (`STUDIO_STUB_BLENDER=1`): sin subproceso; genera outputs simulados.
    - Real: `blender -b --python <script>` con timeout; log completo a
      `blender.log` (append + rotación simple) y cola redactada en el resultado.
    """
    spec.validate()
    started = time.monotonic()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / BLENDER_LOG_NAME
    out_dir = Path(spec.output_dir)

    if is_stub_mode():
        outputs = _stub_outputs_for(spec)
        # el stub también pasa por la validación: la garantía "run_blender_spec
        # nunca devuelve ok con artefactos inválidos" cubre ambos caminos.
        validation_error = validate_blender_outputs(spec, outputs)
        tail = "[STUB] STUDIO_STUB_BLENDER=1: ejecución simulada de Blender.\n"
        if validation_error:
            tail += f"[VALIDACION] {validation_error}\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(tail)
        return BlenderResult(
            exit_code=0,
            outputs=outputs,
            log_tail=tail,
            duration_s=time.monotonic() - started,
            stubbed=True,
            validation_error=validation_error,
        )

    binary = blender_bin or find_blender_bin()
    if binary is None:
        return BlenderResult(
            exit_code=127,
            log_tail=(
                "Blender no encontrado (STUDIO_BLENDER_BIN/PATH//opt/blender). "
                "El perfil 'unreal' instala Blender 4.x en /opt/blender."
            ),
            duration_s=time.monotonic() - started,
        )

    script = (
        build_render_script(spec)
        if isinstance(spec, BlenderRenderSpec)
        else build_export_script(spec)
    )
    script_path = log_dir / "blender_job.py"
    script_path.write_text(script, encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [binary, "-b", "--python", str(script_path)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(out_dir),
        )
    except OSError as exc:
        return BlenderResult(
            exit_code=126,
            log_tail=f"No se pudo ejecutar Blender: {exc}",
            duration_s=time.monotonic() - started,
        )

    timed_out = False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_s)
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout, _ = await proc.communicate()

    redactor = SecretRedactor.from_env()
    raw = redactor.redact((stdout or b"").decode("utf-8", errors="replace"))
    header = (
        f"\n===== BlenderRunner {'TIMEOUT' if timed_out else 'run'} "
        f"({time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}) =====\n"
    )
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(header)
        fh.write(raw)
    # Rotación simple: si el log supera LOG_MAX_MB, conservar la mitad final
    try:
        max_mb = int(os.environ.get("LOG_MAX_MB") or "200")
        if max_mb > 0 and log_path.stat().st_size > max_mb * 1_048_576:
            data = log_path.read_bytes()
            log_path.write_bytes(data[len(data) // 2 :])
    except (OSError, ValueError):
        pass

    exit_code = -1 if timed_out else (proc.returncode if proc.returncode is not None else -1)
    outputs = sorted(p for p in out_dir.iterdir() if p.is_file()) if out_dir.is_dir() else []
    outputs = [
        p for p in outputs if p.name not in ("blender_job.py", "blender.log", BLENDER_LOG_NAME)
    ]
    validation_error = validate_blender_outputs(spec, outputs)
    tail = redactor.redact(raw[-LOG_TAIL_BYTES:])
    if timed_out:
        tail = f"[TIMEOUT tras {spec.timeout_s:.0f}s]\n" + tail
    if validation_error:
        tail = f"[VALIDACION] {validation_error}\n" + tail
    return BlenderResult(
        exit_code=exit_code,
        outputs=outputs,
        log_tail=tail,
        duration_s=time.monotonic() - started,
        validation_error=validation_error,
    )


def build_unreal_manifest(
    *,
    project_name: str,
    files: list[Path],
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Manifiesto del paquete UE-ready: checksums, tamaños y versiones."""
    entries = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(
            {
                "file": path.name,
                "bytes": path.stat().st_size,
                "sha256": digest,
            }
        )
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "generator": "comfy-studio-blenderrunner/m3-d",
        "project": project_name,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": entries,
        "unreal_import_hints": {
            "usd": "Datasmith > Import USD (.usdc) o Content Browser > Import",
            "gltf": "Datasmith glTF importer (Textured/Interchange)",
            "fbx": "FBX estándar con triangulación; ejes +Z up convertidos por UE",
        },
    }
    if extras:
        manifest.update(extras)
    return manifest


def write_unreal_manifest(manifest: dict[str, Any], dest: Path) -> Path:
    dest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return dest


__all__ = [
    "BLENDER_LOG_NAME",
    "FAILURE_INVALID_OUTPUT",
    "BlenderCameraKeyframe",
    "BlenderExportSpec",
    "BlenderRenderSpec",
    "BlenderResult",
    "build_export_script",
    "build_render_script",
    "build_unreal_manifest",
    "classify_blender_failure",
    "find_blender_bin",
    "is_stub_mode",
    "run_blender_spec",
    "validate_blender_outputs",
    "write_unreal_manifest",
]
