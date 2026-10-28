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
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("comfy_studio.blender")

BLENDER_LOG_NAME = "blender.log"
DEFAULT_BLENDER_TIMEOUT_S = 3600.0
LOG_TAIL_BYTES = 4096

_ENGINES = ("cycles", "eevee")
_EXPORT_FORMATS = ("usd", "gltf", "fbx")

# Bytes mínimos válidos-ish para el modo stub (tests): PNG 1x1 y cabeceras.
_STUB_PNG = (
    b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
)
_STUB_GLB_HEADER = b"glTF\x02\x00\x00\x00\x14\x00\x00\x00" + b"\x00" * 12
_STUB_USD = b"#usda 1.0\n( defaultPrim = \"World\" )\ndef Xform \"World\" {}\n"
_STUB_FBX = b"; FBX stub (M3-D STUDIO_STUB_BLENDER)\n"


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
    output_format: str = "png"  # png | exr (secuencia) — el MP4 se compone con ffmpeg
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

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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


def build_render_script(spec: BlenderRenderSpec) -> str:
    """Genera el script Python que Blender ejecuta en modo `-b`."""
    scene = str(spec.scene_path).replace("\\", "/")
    out_dir = str(spec.output_dir).replace("\\", "/")
    engine = "CYCLES" if spec.engine == "cycles" else "BLENDER_EEVEE_NEXT"
    return f'''# Generado por Comfy Studio BlenderRunner (M3-D). No editar a mano.
import bpy, sys

scene_path = r"{scene}"
out_dir = r"{out_dir}"

if scene_path.lower().endswith((".glb", ".gltf")):
    bpy.ops.import_scene.gltf(filepath=scene_path)
elif scene_path.lower().endswith((".usd", ".usda", ".usdc", ".usdz")):
    bpy.ops.wm.usd_import(filepath=scene_path)
elif scene_path.lower().endswith(".obj"):
    bpy.ops.wm.obj_import(filepath=scene_path)
else:
    bpy.ops.wm.open_mainfile(filepath=scene_path)

scene = bpy.context.scene
scene.render.engine = "{engine}"
scene.render.resolution_x = {spec.width}
scene.render.resolution_y = {spec.height}
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = "{"OPEN_EXR" if spec.output_format == "exr" else "PNG"}"
scene.render.filepath = out_dir + "/render_"
scene.frame_start = 1
scene.frame_end = {spec.frames}

if scene.render.engine == "CYCLES":
    scene.cycles.samples = {spec.samples}
    scene.cycles.device = "GPU"
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
        target = {spec.device_index}
        idx = 0
        for d in devices:
            if getattr(d, "type", "") in ("OPTIX", "CUDA"):
                d.use = (idx == target)
                idx += 1
            else:
                d.use = False
else:
    scene.eevee.taa_render_samples = {spec.samples}

bpy.ops.render.render(animation={str(spec.frames > 1).lower()}, write_still=True)
print("[BlenderRunner] render completado:", out_dir)
'''


def build_export_script(spec: BlenderExportSpec) -> str:
    """Genera el script Python de exportación USD/glTF/FBX desde una malla/escena."""
    scene = str(spec.scene_path).replace("\\", "/")
    out_dir = str(spec.output_dir).replace("\\", "/")
    stem = spec.scene_path.stem
    lines = [
        "# Generado por Comfy Studio BlenderRunner (M3-D). No editar a mano.",
        "import bpy",
        "",
        f'scene_path = r"{scene}"',
        f'out_dir = r"{out_dir}"',
        f'stem = r"{stem}"',
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
        'if not bpy.data.objects:',
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
        for i in range(count):
            suffix = f"_{i + 1:04d}" if spec.frames > 1 else ""
            path = out_dir / f"render{suffix}.{ext}"
            path.write_bytes(_STUB_PNG)
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
        tail = "[STUB] STUDIO_STUB_BLENDER=1: ejecución simulada de Blender.\n"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(tail)
        return BlenderResult(
            exit_code=0,
            outputs=outputs,
            log_tail=tail,
            duration_s=time.monotonic() - started,
            stubbed=True,
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

    raw = (stdout or b"").decode("utf-8", errors="replace")
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
    tail = raw[-LOG_TAIL_BYTES:]
    if timed_out:
        tail = f"[TIMEOUT tras {spec.timeout_s:.0f}s]\n" + tail
    return BlenderResult(
        exit_code=exit_code,
        outputs=[p for p in outputs if p.name != "blender_job.py"],
        log_tail=tail,
        duration_s=time.monotonic() - started,
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
    "BlenderExportSpec",
    "BlenderRenderSpec",
    "BlenderResult",
    "BLENDER_LOG_NAME",
    "classify_blender_failure",
    "build_export_script",
    "build_render_script",
    "build_unreal_manifest",
    "find_blender_bin",
    "is_stub_mode",
    "run_blender_spec",
    "write_unreal_manifest",
]
