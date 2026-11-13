"""Animatic CPU (C05/C08/A13): preview real desde claves de cámara interpoladas.

Genera un MP4 local a partir de los keyframes de un ``CameraPlan`` (interpolados),
lo verifica con ``ffprobe`` y conecta el preflight factual/composición
(``check_composition`` + ``assert_factual_camera``) cuando se proporcionan los
datos necesarios. Solo CPU, fixture sintético: no GPU, pesos ni red.
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from .cinematography import (
    CameraError,
    CameraKeyframe,
    CameraPlan,
    assert_factual_camera,
    check_composition,
)
from .media_bins import find_ffmpeg, find_ffprobe
from .perception.keyframes import probe_media_info

Vec3 = tuple[float, float, float]
MAX_ANIMATIC_FPS = 60.0
MAX_ANIMATIC_DURATION_S = 120.0
MAX_ANIMATIC_FRAMES = 900
MAX_ANIMATIC_PIXEL_FRAMES = 250_000_000


class AnimaticError(ValueError):
    """Fallo al generar o verificar el animatic."""


def _keyframes(plan_or_keys: Any) -> list[CameraKeyframe]:
    if hasattr(plan_or_keys, "keyframes"):
        return list(plan_or_keys.keyframes)
    return list(plan_or_keys)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_vec(a: Vec3, b: Vec3, t: float) -> Vec3:
    return (_lerp(a[0], b[0], t), _lerp(a[1], b[1], t), _lerp(a[2], b[2], t))


def _validate_camera_plan(plan: CameraPlan) -> None:
    try:
        plan.validate()
    except CameraError as exc:
        raise AnimaticError(str(exc)) from exc
    _validate_keyframes(plan.keyframes)
    if not 0 < float(plan.duration_s) <= MAX_ANIMATIC_DURATION_S:
        raise AnimaticError(f"duration_s debe estar entre 0 y {MAX_ANIMATIC_DURATION_S}.")


def _validate_keyframes(keys: list[CameraKeyframe]) -> None:
    if not 1 <= len(keys) <= 120:
        raise AnimaticError("se requiere de 1 a 120 keyframes")
    previous_t = -1.0
    for key in keys:
        values = (float(key.t), *key.position, *key.target)
        if not all(math.isfinite(float(value)) for value in values):
            raise AnimaticError("los keyframes deben contener solo valores finitos")
        if not 0.0 <= key.t <= 1.0 or key.t <= previous_t:
            raise AnimaticError("los tiempos de keyframe deben crecer estrictamente en [0, 1]")
        previous_t = key.t


def interpolate_camera_at(plan_or_keys: Any, t: float) -> tuple[Vec3, Vec3]:
    """Interpola posición y target para un tiempo normalizado ``t`` (0..1)."""
    keys = _keyframes(plan_or_keys)
    _validate_keyframes(keys)
    t = max(0.0, min(1.0, float(t)))
    if t <= keys[0].t:
        return keys[0].position, keys[0].target
    if t >= keys[-1].t:
        return keys[-1].position, keys[-1].target
    for i in range(len(keys) - 1):
        a, b = keys[i], keys[i + 1]
        if a.t <= t <= b.t:
            span = b.t - a.t
            if span <= 0:
                return a.position, a.target
            local = (t - a.t) / span
            return _lerp_vec(a.position, b.position, local), _lerp_vec(
                a.target, b.target, local
            )
    return keys[-1].position, keys[-1].target


def interpolate_camera(plan_or_keys: Any, t: float) -> dict[str, Any]:
    """Variante dict de :func:`interpolate_camera_at`."""
    pos, tgt = interpolate_camera_at(plan_or_keys, t)
    return {"t": float(t), "position": list(pos), "target": list(tgt)}


def sample_camera_path(
    plan_or_keys: Any,
    *,
    fps: float | None = None,
    duration_s: float | None = None,
    frame_count: int | None = None,
) -> list[dict[str, Any]]:
    """Muestrea la trayectoria interpolada en ``frame_count`` fotogramas."""
    plan = plan_or_keys
    if duration_s is None:
        duration_s = getattr(plan, "duration_s", 1.0)
    duration_s = float(duration_s)
    if not 0 < duration_s <= MAX_ANIMATIC_DURATION_S:
        raise AnimaticError(f"duration_s debe estar entre 0 y {MAX_ANIMATIC_DURATION_S}.")
    if fps is not None and not 1 <= float(fps) <= MAX_ANIMATIC_FPS:
        raise AnimaticError(f"fps debe estar entre 1 y {MAX_ANIMATIC_FPS}.")
    if frame_count is None:
        if fps is None:
            fps = 12.0
        fps = float(fps)
        if not 1 <= fps <= MAX_ANIMATIC_FPS:
            raise AnimaticError(f"fps debe estar entre 1 y {MAX_ANIMATIC_FPS}.")
        frame_count = max(1, int(round(duration_s * fps)))
    else:
        if isinstance(frame_count, bool) or int(frame_count) != frame_count:
            raise AnimaticError("frame_count debe ser un entero positivo.")
        frame_count = int(frame_count)
    if not 1 <= frame_count <= MAX_ANIMATIC_FRAMES:
        raise AnimaticError(f"frame_count debe estar entre 1 y {MAX_ANIMATIC_FRAMES}.")
    samples: list[dict[str, Any]] = []
    for i in range(frame_count):
        if frame_count == 1:
            t = 0.0
        else:
            t = i / (frame_count - 1)
        pos, tgt = interpolate_camera_at(plan_or_keys, t)
        samples.append(
            {
                "index": i,
                "t": round(t, 6),
                "time_s": round(t * duration_s, 6),
                "position": [float(x) for x in pos],
                "target": [float(x) for x in tgt],
            }
        )
    return samples


def encode_camera_trajectory_frames(
    frames: list[str | Path],
    output_path: str | Path,
    *,
    fps: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Encode Blender camera frames as a verified MP4; never trusts file names alone."""
    fps, width, height = float(fps), int(width), int(height)
    if not 1 <= fps <= MAX_ANIMATIC_FPS:
        raise AnimaticError(f"fps debe estar entre 1 y {MAX_ANIMATIC_FPS}.")
    if width < 16 or height < 16 or width > 3840 or height > 2160 or width % 2 or height % 2:
        raise AnimaticError("dimensiones inválidas para codificación yuv420p.")
    if not 1 <= len(frames) <= MAX_ANIMATIC_FRAMES:
        raise AnimaticError(f"se requiere de 1 a {MAX_ANIMATIC_FRAMES} fotogramas.")
    try:
        from PIL import Image

        for frame in frames:
            with Image.open(frame) as image:
                image.verify()
            with Image.open(frame) as image:
                if image.size != (width, height):
                    raise AnimaticError(
                        f"PNG de cámara inválido: {frame} mide {image.width}x{image.height}."
                    )
    except AnimaticError:
        raise
    except (OSError, ValueError) as exc:
        raise AnimaticError(f"PNG de cámara inválido: {exc}") from exc

    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise AnimaticError("se requiere ffmpeg y ffprobe en PATH")
    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="comfy_camera_encode_") as tmp:
        sequence = Path(tmp)
        for index, frame in enumerate(frames, start=1):
            (sequence / f"frame_{index:04d}.png").write_bytes(Path(frame).read_bytes())
        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", str(fps),
            "-i", str(sequence / "frame_%04d.png"), "-frames:v", str(len(frames)),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnimaticError(f"ffmpeg falló: {exc}") from exc
        if result.returncode != 0:
            raise AnimaticError(f"ffmpeg falló (exit {result.returncode}): {result.stderr[-1200:]}")
    probe = probe_media_info(output)
    if not probe.get("probe_ok") or not probe.get("has_video"):
        raise AnimaticError(f"ffprobe no pudo verificar el animatic: {probe}")
    if int(probe.get("width", 0)) != width or int(probe.get("height", 0)) != height:
        raise AnimaticError("ffprobe devolvió dimensiones inesperadas")
    count_command = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-count_frames",
        "-show_entries", "stream=nb_read_frames",
        "-of", "json",
        str(output),
    ]
    try:
        count_result = subprocess.run(
            count_command, capture_output=True, text=True, timeout=60, check=False
        )
        counted = json.loads(count_result.stdout) if count_result.returncode == 0 else {}
        decoded_frames = int((counted.get("streams") or [{}])[0].get("nb_read_frames", 0))
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
        raise AnimaticError(f"ffprobe no pudo contar fotogramas: {exc}") from exc
    if decoded_frames != len(frames):
        raise AnimaticError(
            f"ffprobe contó {decoded_frames} fotogramas; se esperaban {len(frames)}"
        )
    expected_duration = len(frames) / fps
    duration = float(probe.get("duration_sec", probe.get("duration_s", 0.0)))
    if duration <= 0 or abs(duration - expected_duration) > max(0.1, 1.0 / fps):
        raise AnimaticError(f"ffprobe devolvió duración inesperada: {duration}")
    return {
        "output": str(output), "probe_ok": True, "frame_count": decoded_frames,
        "duration_s": duration, "fps": fps, "width": width, "height": height,
        "has_video": True, "has_audio": bool(probe.get("has_audio")), "probe": probe,
    }


def check_animatic_preflight(
    plan: CameraPlan,
    *,
    subject_position: Vec3 | None = None,
    subject_radius: float = 0.5,
    obstacles: list[tuple[Vec3, float]] | None = None,
    min_distance: float = 0.5,
    asset: Any = None,
    part_id: str | None = None,
) -> dict[str, Any]:
    """Preflight factual + composición (fail-closed)."""
    _validate_camera_plan(plan)
    errors: list[str] = []
    composition: dict[str, Any] | None = None
    if subject_position is not None:
        report = check_composition(
            plan,
            subject_position=subject_position,
            subject_radius=subject_radius,
            min_distance=min_distance,
            obstacles=obstacles,
        )
        composition = report.to_dict()
        if not report.ok:
            errors.extend(report.collisions + report.occlusions)
    factual: dict[str, Any] = {"checked": False}
    if plan.factual and (asset is None or not part_id):
        factual = {
            "checked": True,
            "ok": False,
            "error": "una cámara factual requiere asset y part_id observados",
        }
        errors.append(factual["error"])
    elif plan.factual:
        try:
            assert_factual_camera(plan, asset, part_id)
            factual = {"checked": True, "ok": True}
        except CameraError as exc:
            factual = {"checked": True, "ok": False, "error": str(exc)}
            errors.append(str(exc))
    ok = (composition is None or composition.get("ok", True)) and (
        not factual.get("checked") or factual.get("ok", True)
    )
    return {"ok": bool(ok), "composition": composition, "factual": factual, "errors": errors}


def _top_down_points(
    samples: list[dict[str, Any]], subject_position: Vec3 | None
) -> tuple[list[tuple[float, float]], tuple[float, float] | None]:
    pts: list[tuple[float, float]] = []
    xs = [s["position"][0] for s in samples]
    zs = [s["position"][2] for s in samples]
    if subject_position is not None:
        xs.append(subject_position[0])
        zs.append(subject_position[2])
    if not xs:
        return pts, None
    min_x, max_x = min(xs), max(xs)
    min_z, max_z = min(zs), max(zs)
    span = max(max_x - min_x, max_z - min_z, 1e-3)
    margin = span * 0.2 + 1.0
    min_x -= margin
    max_x += margin
    min_z -= margin
    max_z += margin
    span_x = max(max_x - min_x, 1e-3)
    span_z = max(max_z - min_z, 1e-3)

    def to_screen(x: float, z: float) -> tuple[float, float]:
        sx = (x - min_x) / span_x
        sz = (z - min_z) / span_z
        return sx, 1.0 - sz

    for s in samples:
        pts.append(to_screen(s["position"][0], s["position"][2]))
    subj = None
    if subject_position is not None:
        subj = to_screen(subject_position[0], subject_position[2])
    return pts, subj


def render_animatic(
    plan: CameraPlan,
    output_path: str | Path,
    *,
    fps: float = 12.0,
    width: int = 640,
    height: int = 360,
    duration_s: float | None = None,
    subject_position: Vec3 | None = None,
    subject_radius: float = 0.5,
    obstacles: list[tuple[Vec3, float]] | None = None,
    min_distance: float = 0.5,
    asset: Any = None,
    part_id: str | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Renderiza un MP4 de animatic desde claves de cámara interpoladas.

    Verifica el resultado con ``ffprobe``. Falla si falta ffmpeg/ffprobe o si el
    preflight factual/composición no pasa.
    """
    _validate_camera_plan(plan)
    if duration_s is None:
        duration_s = plan.duration_s
    duration_s = float(duration_s)
    fps = float(fps)
    width = int(width)
    height = int(height)
    if not 1 <= fps <= MAX_ANIMATIC_FPS:
        raise AnimaticError(f"fps debe estar entre 1 y {MAX_ANIMATIC_FPS}.")
    if not 0 < duration_s <= MAX_ANIMATIC_DURATION_S:
        raise AnimaticError(f"duration_s debe estar entre 0 y {MAX_ANIMATIC_DURATION_S}.")
    if width < 16 or height < 16 or width > 3840 or height > 2160:
        raise AnimaticError("dimensiones deben estar entre 16x16 y 3840x2160.")
    if width % 2 or height % 2:
        raise AnimaticError("las dimensiones deben ser pares para codificación yuv420p.")
    estimated_frames = max(1, int(round(duration_s * fps)))
    if estimated_frames > MAX_ANIMATIC_FRAMES:
        raise AnimaticError(f"máximo {MAX_ANIMATIC_FRAMES} fotogramas por animatic.")
    if estimated_frames * width * height > MAX_ANIMATIC_PIXEL_FRAMES:
        raise AnimaticError("el presupuesto máximo de píxeles por animatic se excede.")

    preflight: dict[str, Any] | None = None
    if subject_position is not None:
        preflight = check_animatic_preflight(
            plan,
            subject_position=subject_position,
            subject_radius=subject_radius,
            obstacles=obstacles,
            min_distance=min_distance,
            asset=asset,
            part_id=part_id,
        )
        if not preflight["ok"]:
            raise AnimaticError(
                "preflight de animatic falló: " + "; ".join(preflight["errors"])
            )
    elif plan.factual:
        preflight = check_animatic_preflight(
            plan, asset=asset, part_id=part_id
        )
        if not preflight["ok"]:
            raise AnimaticError(
                "preflight de animatic falló: " + "; ".join(preflight["errors"])
            )

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise AnimaticError("se requiere ffmpeg y ffprobe en PATH")

    samples = sample_camera_path(plan, fps=fps, duration_s=duration_s)
    if not samples:
        raise AnimaticError("no se generaron muestras de cámara")

    pts, subj_pt = _top_down_points(samples, subject_position)

    output = Path(output_path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="comfy_animatic_") as tmp:
        tmp_dir = Path(tmp)
        for i, s in enumerate(samples):
            img = Image.new("RGB", (width, height), (18, 22, 30))
            draw = ImageDraw.Draw(img)
            for gx in range(0, width, max(1, width // 8)):
                draw.line([(gx, 0), (gx, height)], fill=(30, 36, 46))
            for gy in range(0, height, max(1, height // 6)):
                draw.line([(0, gy), (width, gy)], fill=(30, 36, 46))

            screen_pts = [
                (int(p[0] * (width - 40) + 20), int(p[1] * (height - 40) + 20))
                for p in pts
            ]
            if len(screen_pts) > 1:
                draw.line(screen_pts, fill=(90, 120, 170), width=2)
            for sp in screen_pts:
                draw.ellipse((sp[0] - 2, sp[1] - 2, sp[0] + 2, sp[1] + 2), fill=(90, 120, 170))

            if subj_pt is not None:
                sx = int(subj_pt[0] * (width - 40) + 20)
                sy = int(subj_pt[1] * (height - 40) + 20)
                r = max(4, int(subject_radius * 20))
                draw.ellipse((sx - r, sy - r, sx + r, sy + r), outline=(220, 180, 80), width=2)

            if screen_pts:
                cx, cy = screen_pts[i]
                draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=(240, 90, 90))
                tgt = s["target"]
                tx = cx + int((tgt[0] - s["position"][0]) * 8)
                ty = cy + int((tgt[2] - s["position"][2]) * -8)
                draw.line([(cx, cy), (tx, ty)], fill=(240, 90, 90), width=1)

            lines = [
                f"ANIMATIC (sintético, CPU) - {title or plan.shot_id}",
                f"frame {i + 1}/{len(samples)}  t={s['t']:.3f}  time={s['time_s']:.2f}s",
                f"focal={plan.focal_mm:.1f}mm  fov={plan.fov_deg():.1f}°  dur={duration_s:.2f}s",
                f"pos=({s['position'][0]:.2f},{s['position'][1]:.2f},{s['position'][2]:.2f})",
            ]
            y = 8
            for line in lines:
                draw.text((8, y), line, fill=(230, 235, 245))
                y += 14

            frame_path = tmp_dir / f"frame_{i + 1:04d}.png"
            img.save(frame_path, format="PNG")

        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-framerate",
            str(fps),
            "-i",
            str(tmp_dir / "frame_%04d.png"),
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AnimaticError(f"ffmpeg falló: {exc}") from exc
        if result.returncode != 0:
            raise AnimaticError(
                f"ffmpeg falló (exit {result.returncode}): {result.stderr[-1200:]}"
            )

    probe = probe_media_info(output)
    if not probe.get("probe_ok"):
        raise AnimaticError(
            f"ffprobe no pudo verificar el animatic: {probe}"
        )
    if not probe.get("has_video"):
        raise AnimaticError("ffprobe: el animatic no tiene pista de vídeo")
    if int(probe.get("width", 0)) != width or int(probe.get("height", 0)) != height:
        raise AnimaticError(
            f"ffprobe: dimensiones {probe.get('width')}x{probe.get('height')} "
            f"!= {width}x{height}"
        )

    interpolated_keys = [
        {"t": s["t"], "position": s["position"], "target": s["target"]}
        for s in samples
    ]
    return {
        "output": str(output),
        "frames": len(samples),
        "fps": fps,
        "duration_s": duration_s,
        "width": width,
        "height": height,
        "interpolated_keys": interpolated_keys,
        "probe": probe,
        "probe_ok": bool(probe.get("probe_ok")),
        "preflight": preflight,
        "synthetic": True,
        "quality_claim": False,
    }


# Aliases / nombres alternativos para compatibilidad con tests y callers.
interpolate_camera_keys = interpolate_camera_at
interpolate = interpolate_camera_at
sample_camera = sample_camera_path
render_animatic_preview = render_animatic
render_preview = render_animatic
preflight_animatic = check_animatic_preflight
animatic_preflight = check_animatic_preflight
render = render_animatic

__all__ = [
    "AnimaticError",
    "interpolate_camera",
    "interpolate_camera_at",
    "interpolate_camera_keys",
    "interpolate",
    "sample_camera_path",
    "sample_camera",
    "check_animatic_preflight",
    "preflight_animatic",
    "animatic_preflight",
    "render_animatic",
    "render_animatic_preview",
    "render_preview",
    "render",
]
