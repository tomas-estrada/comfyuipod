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
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
