# ruff: noqa: E501
"""Real headless Blender CPU path for editable skeleton and deformation GLBs."""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .blender import find_blender_bin, is_stub_mode
from .motion import MotionClip, Skeleton


class BlenderRigError(ValueError):
    pass


@dataclass(slots=True)
class BlenderRigSpec:
    skeleton: Skeleton
    clip: MotionClip
    output_dir: Path
    timeout_s: float = 180.0

    def validate(self) -> None:
        self.skeleton.validate()
        self.clip.validate(self.skeleton)
        if not self.clip.frames:
            raise BlenderRigError("rig export requires at least one pose frame")
        if len(self.clip.frames) > 600:
            raise BlenderRigError("rig export is limited to 600 pose frames")
        limits = {bone.name: (bone.min_deg, bone.max_deg) for bone in self.skeleton.bones}
        for frame_index, frame in enumerate(self.clip.frames):
            for bone_name, angle in frame.items():
                low, high = limits[bone_name]
                if not low <= angle <= high:
                    raise BlenderRigError(
                        f"frame {frame_index} bone {bone_name!r} exceeds joint limit [{low}, {high}]"
                    )


@dataclass(slots=True)
class BlenderRigResult:
    ok: bool
    glb_path: Path | None = None
    report: dict[str, Any] | None = None
    error: str | None = None


def build_rig_script(spec_path: Path, report_path: Path, glb_path: Path) -> str:
    """Generate Blender-owned armature, weighted segmented mesh, and animation."""
    return f"""import bpy, json, math
from mathutils.bvhtree import BVHTree
with open({str(spec_path)!r}, encoding="utf-8") as f: cfg=json.load(f)
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete(use_global=False)
scene=bpy.context.scene; scene.unit_settings.system="METRIC"; scene.render.fps=cfg["clip"]["fps"]
bones=cfg["skeleton"]["bones"]; root=cfg["skeleton"]["root"]
arm_data=bpy.data.armatures.new(cfg["skeleton"]["name"]); arm=bpy.data.objects.new(cfg["skeleton"]["name"], arm_data); scene.collection.objects.link(arm)
bpy.context.view_layer.objects.active=arm; arm.select_set(True); bpy.ops.object.mode_set(mode="EDIT")
edit={{}}
for index,b in enumerate(bones):
    parent=edit.get(b.get("parent")); depth=0; cursor=b.get("parent")
    while cursor:
        depth+=1; cursor=next((item.get("parent") for item in bones if item["name"]==cursor),None)
    eb=arm_data.edit_bones.new(b["name"]); eb.head=(depth*0.35,0,index*0.06); eb.tail=(depth*0.35,0,index*0.06+0.32)
    eb.parent=parent; eb.use_connect=False; edit[b["name"]]=eb
bpy.ops.object.mode_set(mode="OBJECT")
for bone_name,angle in cfg["skeleton"].get("bind",{{}}).items():
    pb=arm.pose.bones.get(bone_name); pb.rotation_mode="XYZ"; pb.rotation_euler[1]=math.radians(float(angle))
if cfg["skeleton"].get("bind"):
    bpy.ops.object.mode_set(mode="POSE"); bpy.ops.pose.armature_apply(selected=False); bpy.ops.object.mode_set(mode="OBJECT")
verts=[]; faces=[]; assignments=[]
for index,b in enumerate(bones):
    x=index*0.42; y=0; z=0.2
    base=len(verts); verts.extend([(x+dx,y+dy,z+dz) for dx,dy,dz in [(-.12,-.12,-.16),(.12,-.12,-.16),(.12,.12,-.16),(-.12,.12,-.16),(-.12,-.12,.16),(.12,-.12,.16),(.12,.12,.16),(-.12,.12,.16)]])
    faces.extend([tuple(base+i for i in face) for face in [(0,1,2,3),(4,7,6,5),(0,4,5,1),(1,5,6,2),(2,6,7,3),(4,0,3,7)]])
    assignments.extend([b["name"]]*8)
mesh=bpy.data.meshes.new("DeformMesh"); mesh.from_pydata(verts,[],faces); mesh.update(); obj=bpy.data.objects.new("DeformMesh",mesh); scene.collection.objects.link(obj)
for b in bones:
    group=obj.vertex_groups.new(name=b["name"]); indices=[i for i,n in enumerate(assignments) if n==b["name"]]; group.add(indices,1.0,"REPLACE")
modifier=obj.modifiers.new("ArmatureDeform","ARMATURE"); modifier.object=arm; obj.parent=arm
scene.frame_start=1; scene.frame_end=len(cfg["clip"]["frames"])
for frame_index,pose in enumerate(cfg["clip"]["frames"], start=1):
    scene.frame_set(frame_index)
    for bone_name,angle in pose.items():
        pb=arm.pose.bones.get(bone_name)
        if pb:
            bind=float(cfg["skeleton"].get("bind",{{}}).get(bone_name,0)); pb.rotation_mode="XYZ"; pb.rotation_euler[1]=math.radians(float(angle)-bind); pb.keyframe_insert(data_path="rotation_euler",frame=frame_index)
    root_position=cfg["clip"].get("root_track",[])
    if root_position: arm.location=tuple(root_position[frame_index-1]); arm.keyframe_insert(data_path="location",frame=frame_index)
# Sample every pose and report evaluated deformation magnitude against the rest mesh.
rest=[v.co.copy() for v in mesh.vertices]; max_disp=0.0; self_intersection_triangle_pairs=0
for frame in range(1,len(cfg["clip"]["frames"])+1):
    scene.frame_set(frame); deps=bpy.context.evaluated_depsgraph_get(); evaluated=obj.evaluated_get(deps); evaluated_mesh=evaluated.to_mesh()
    for before,after in zip(rest,evaluated_mesh.vertices): max_disp=max(max_disp,(after.co-before).length)
    trees={{}}
    for bone_name in set(assignments):
        indices=[i for i,name in enumerate(assignments) if name==bone_name]; lookup={{old:new for new,old in enumerate(indices)}}
        polygons=[tuple(lookup[i] for i in polygon.vertices) for polygon in evaluated_mesh.polygons if all(i in lookup for i in polygon.vertices)]
        if polygons: trees[bone_name]=BVHTree.FromPolygons([evaluated_mesh.vertices[i].co for i in indices],polygons)
    names=list(trees)
    for left_index,left_name in enumerate(names):
        for right_name in names[left_index+1:]:
            self_intersection_triangle_pairs+=len(trees[left_name].overlap(trees[right_name]))
    evaluated.to_mesh_clear()
scene.frame_set(1)
bpy.ops.object.select_all(action="DESELECT"); arm.select_set(True); obj.select_set(True); bpy.context.view_layer.objects.active=arm
bpy.ops.export_scene.gltf(filepath={str(glb_path)!r},export_format="GLB",export_animations=True,export_skins=True,export_apply=False)
# Reimport the exported GLB into a clean scene and confirm skeleton, skin, and animation survived.
bpy.ops.object.select_all(action="SELECT"); bpy.ops.object.delete(use_global=False); bpy.ops.import_scene.gltf(filepath={str(glb_path)!r})
reimport_arm=next((o for o in bpy.context.scene.objects if o.type=="ARMATURE"),None)
skinned=[o for o in bpy.context.scene.objects if o.type=="MESH" and any(m.type=="ARMATURE" for m in o.modifiers)]
animations=len(bpy.data.actions)
report={{"schema":"comfy_studio.motion-rig.v1","bones":len(bones),"bind_pose":cfg["skeleton"].get("bind",{{}}),"bind_pose_applied":True,"frame_count":len(cfg["clip"]["frames"]),"fps":cfg["clip"]["fps"],"extreme_pose_deformation_max_m":max_disp,"reimport_bones":len(reimport_arm.data.bones) if reimport_arm else 0,"reimport_skinned_meshes":len(skinned),"reimport_animations":animations,"self_intersection_triangle_pairs":self_intersection_triangle_pairs,"penetration_method":"per-frame BVH overlap between weighted bone mesh segments; triangle-pair count, not depth","gpu_validation":"not_run"}}
with open({str(report_path)!r},"w",encoding="utf-8") as f: json.dump(report,f,sort_keys=True)
if not reimport_arm or len(reimport_arm.data.bones)!=len(bones) or not skinned or animations<1: raise RuntimeError("GLB reimport lost rig, skin, or animation")
"""


async def run_blender_rig(spec: BlenderRigSpec, blender_bin: str | None = None) -> BlenderRigResult:
    spec.validate()
    if is_stub_mode():
        return BlenderRigResult(
            False, error="STUDIO_STUB_BLENDER is enabled; no CPU rig artifact created"
        )
    binary = blender_bin or find_blender_bin()
    if not binary:
        return BlenderRigResult(
            False, error="Blender binary not found; CPU rig artifact not created"
        )
    spec.output_dir.mkdir(parents=True, exist_ok=True)
    glb = spec.output_dir / "motion-rig.glb"
    report_path = spec.output_dir / "motion-rig-report.json"
    with tempfile.TemporaryDirectory(prefix="comfy_motion_script_") as folder:
        spec_path = Path(folder) / "spec.json"
        spec_path.write_text(
            json.dumps(
                {
                    "skeleton": {
                        "name": spec.skeleton.name,
                        "root": spec.skeleton.root,
                        "scale": spec.skeleton.scale,
                        "bind": spec.skeleton.bind,
                        "bones": [
                            {
                                "name": b.name,
                                "parent": b.parent,
                                "min_deg": b.min_deg,
                                "max_deg": b.max_deg,
                            }
                            for b in spec.skeleton.bones
                        ],
                    },
                    "clip": spec.clip.to_dict(),
                },
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        script = Path(folder) / "build.py"
        script.write_text(build_rig_script(spec_path, report_path, glb), encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "-b",
                "--python",
                str(script),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=spec.timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            return BlenderRigResult(False, error=f"Blender rig process exceeded {spec.timeout_s}s")
        except OSError as exc:
            return BlenderRigResult(False, error=f"Blender rig process failed: {exc}")
        if proc.returncode != 0:
            return BlenderRigResult(False, error=(out or b"").decode("utf-8", "replace")[-4000:])
    if not glb.is_file() or glb.stat().st_size < 20:
        return BlenderRigResult(False, error="Blender did not produce a GLB")
    header = glb.read_bytes()[:12]
    if (
        header[:4] != b"glTF"
        or struct.unpack_from("<I", header, 4)[0] != 2
        or struct.unpack_from("<I", header, 8)[0] != glb.stat().st_size
    ):
        return BlenderRigResult(False, error="Blender rig GLB header invalid")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return BlenderRigResult(
            False,
            error=f"invalid Blender rig report: {exc}; {(out or b'').decode('utf-8', 'replace')[-3000:]}",
        )
    return BlenderRigResult(True, glb_path=glb, report=report)


__all__ = [
    "BlenderRigError",
    "BlenderRigResult",
    "BlenderRigSpec",
    "build_rig_script",
    "run_blender_rig",
]
