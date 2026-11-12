#!/usr/bin/env python3
"""Run a real, tiny Blender CPU render through the Studio engine adapter."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageStat

from comfy_studio.blender import BlenderRenderSpec, run_blender_spec


def _make_scene(blender: str, scene_path: Path, work: Path) -> None:
    script = work / "make_scene.py"
    script.write_text(
        """import bpy
from mathutils import Vector

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 0))
subject = bpy.context.object
subject.name = 'cpu-smoke-subject'
material = bpy.data.materials.new('cpu-smoke-matte')
material.diffuse_color = (0.08, 0.42, 0.78, 1.0)
subject.data.materials.append(material)

camera_data = bpy.data.cameras.new('cpu-smoke-camera')
camera = bpy.data.objects.new('cpu-smoke-camera', camera_data)
scene.collection.objects.link(camera)
camera.location = (3.2, -4.0, 2.4)
camera.rotation_euler = (Vector((0, 0, 0)) - camera.location).to_track_quat('-Z', 'Y').to_euler()
scene.camera = camera

light_data = bpy.data.lights.new('cpu-smoke-key', type='AREA')
light_data.energy = 450
light = bpy.data.objects.new('cpu-smoke-key', light_data)
scene.collection.objects.link(light)
light.location = (1.5, -3.0, 4.0)

world = bpy.data.worlds.new('cpu-smoke-world')
world.color = (0.025, 0.025, 0.025)
scene.world = world
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
            "Blender could not prepare the CPU smoke scene: "
            f"exit={result.returncode}; {result.stderr[-1200:]} {result.stdout[-1200:]}"
        )


def main() -> int:
    blender = os.environ.get("STUDIO_BLENDER_BIN", "/opt/blender/blender")
    os.environ["STUDIO_BLENDER_BIN"] = blender
    os.environ.pop("STUDIO_STUB_BLENDER", None)
    version = subprocess.run(
        [blender, "--version"], capture_output=True, text=True, timeout=30, check=True
    ).stdout.splitlines()[0]
    with tempfile.TemporaryDirectory(prefix="studio-blender-cpu-") as temporary:
        root = Path(temporary)
        scene_path = root / "smoke.blend"
        _make_scene(blender, scene_path, root)
        spec = BlenderRenderSpec(
            scene_path=scene_path,
            output_dir=root / "render",
            engine="cycles",
            samples=2,
            width=96,
            height=64,
            frames=1,
            device="cpu",
            timeout_s=120,
        )
        result = asyncio.run(run_blender_spec(spec, log_dir=root / "logs", blender_bin=blender))
        if not result.ok or result.stubbed:
            raise RuntimeError(
                "Blender CPU engine run did not produce a real render: "
                f"exit={result.exit_code}, stubbed={result.stubbed}, "
                f"validation={result.validation_error}, log={result.log_tail[-1200:]}"
            )
        pngs = [path for path in result.outputs if path.suffix.lower() == ".png"]
        if len(pngs) != 1:
            raise RuntimeError(f"expected exactly one PNG render, got {len(pngs)}")
        with Image.open(pngs[0]) as image:
            image.load()
            if image.size != (96, 64):
                raise RuntimeError(f"render dimensions are {image.size}, expected (96, 64)")
            if max(ImageStat.Stat(image.convert("RGB")).var) <= 0:
                raise RuntimeError("CPU render contains no image variation")
        print(
            json.dumps(
                {
                    "engine": "blender",
                    "version": version,
                    "device": "cpu",
                    "stubbed": False,
                    "outputs": len(pngs),
                    "dimensions": [96, 64],
                    "bytes": pngs[0].stat().st_size,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
