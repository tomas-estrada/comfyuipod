"""Restilización CPU dirigida por el usuario (C22): gradación con LUT 3D local.

Ruta explícita y acotada: el usuario aporta el contenido ``.cube`` de un LUT 3D
local y opcionalmente una máscara (asset de imagen del proyecto). La salida es
un artefacto real (PNG o ffv1 bgr0 lossless) con provenance (sha256 de fuente,
LUT y máscara) y una métrica temporal de estabilidad medida sobre píxeles
decodificados. Esto NO es transferencia de estilo neuronal ni ningún modelo:
solo la transformación de color declarada por el usuario.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageStat

from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.perception.keyframes import probe_media_info
from comfy_studio.projects import ProjectStore


class LutGradeError(ValueError):
    """La gradación con LUT no puede ejecutarse de forma segura sobre la entrada."""


#: Presupuesto de trabajo: la ruta es CPU y debe terminar siempre.
MAX_LUT_DURATION_S = 30.0
MAX_LUT_WIDTH = 1920
MAX_LUT_HEIGHT = 1080
MAX_LUT_FRAMES = 900
MAX_LUT_PIXEL_FRAMES = 250_000_000

#: Límites del parser ``.cube`` acotado.
MAX_LUT_BYTES = 8 * 1024 * 1024
MAX_LUT_GRID = 65
MIN_LUT_GRID = 2

#: Pares adyacentes muestreados para la métrica temporal (≤ 8 pares).
MAX_STABILITY_PAIRS = 8

DEFAULT_STRENGTH = 1.0


def parse_lut_cube(text: Any) -> dict[str, Any]:
    """Parser acotado de un LUT 3D ``.cube`` suministrado inline.

    Acepta solo LUTs 3D (``LUT_3D_SIZE``), grid en
    ``[MIN_LUT_GRID, MAX_LUT_GRID]``, ≤ ``MAX_LUT_BYTES`` de texto, y valores
    finitos. Devuelve ``{"grid", "lines", "size_bytes"}`` o lanza ``ValueError``.
    """
    if not isinstance(text, str):
        raise LutGradeError("el LUT debe ser el texto .cube (cadena)")
    size_bytes = len(text.encode("utf-8", errors="replace"))
    if size_bytes > MAX_LUT_BYTES:
        raise LutGradeError(
            f"LUT demasiado grande ({size_bytes} bytes > {MAX_LUT_BYTES})"
        )
    grid: int | None = None
    data_lines = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keyword = parts[0].upper()
        if keyword == "LUT_3D_SIZE":
            if len(parts) != 2:
                raise LutGradeError("LUT_3D_SIZE requiere exactamente un valor")
            try:
                grid = int(parts[1])
            except ValueError as exc:
                raise LutGradeError(f"LUT_3D_SIZE inválido: {parts[1]!r}") from exc
            if not MIN_LUT_GRID <= grid <= MAX_LUT_GRID:
                raise LutGradeError(
                    f"LUT grid {grid} fuera del rango acotado "
                    f"[{MIN_LUT_GRID}, {MAX_LUT_GRID}]"
                )
        elif keyword in {"LUT_1D_SIZE", "LUT_1D_INPUT_LENGTH", "LUT_1D_OUTPUT_LENGTH"}:
            raise LutGradeError("solo se aceptan LUTs 3D; LUT 1D rechazado")
        elif keyword in {"TITLE", "DOMAIN_MIN", "DOMAIN_MAX"}:
            continue
        else:
            # Línea de datos: tres valores flotantes finitos en [0, 1] ± holgura.
            if len(parts) != 3:
                raise LutGradeError(f"línea de datos LUT inválida: {line[:80]!r}")
            try:
                values = [float(part) for part in parts]
            except ValueError as exc:
                raise LutGradeError(f"línea de datos LUT inválida: {line[:80]!r}") from exc
            if not all(math.isfinite(value) for value in values):
                raise LutGradeError(f"valor LUT no finito: {line[:80]!r}")
            if any(not -1e-6 <= value <= 1.0 + 1e-6 for value in values):
                raise LutGradeError(f"valor LUT fuera de [0, 1]: {line[:80]!r}")
            data_lines += 1
    if grid is None:
        raise LutGradeError("no es un LUT .cube válido: falta LUT_3D_SIZE")
    if data_lines != grid**3:
        raise LutGradeError(
            f"LUT 3D incompleto: se esperan {grid**3} líneas de datos, hay {data_lines}"
        )
    return {"grid": grid, "lines": data_lines, "size_bytes": size_bytes}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_strength(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LutGradeError("strength debe ser un número") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise LutGradeError(f"strength debe estar en [0, 1], es {number}")
    return number


def _image_dimensions(path: Path, label: str) -> tuple[int, int]:
    try:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
    except (OSError, Image.DecompressionBombError) as exc:
        raise LutGradeError(f"{label}: imagen ilegible ({exc})") from exc
    if width <= 0 or height <= 0:
        raise LutGradeError(f"{label}: dimensiones inválidas {width}x{height}")
    return width, height


def _mask_mode(mask_path: Path) -> str:
    """Regla determinista: alpha si es no uniforme, luma en caso contrario."""
    try:
        with Image.open(mask_path) as image:
            image.load()
            if image.mode in {"RGBA", "LA", "PA"} or (
                image.mode == "P" and "transparency" in image.info
            ):
                alpha = image.convert("RGBA").getchannel("A")
                lo, hi = alpha.getextrema()
                if lo != hi:
                    return "alpha"
    except (OSError, Image.DecompressionBombError) as exc:
        raise LutGradeError(f"máscara: imagen ilegible ({exc})") from exc
    return "luma"


def _probe_output(path: Path, *, timeout_s: float) -> dict[str, Any]:
    """ffprobe directo del resultado: códec, pix_fmt, fps y nº de frames."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise LutGradeError("se requiere ffprobe en PATH")
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-count_frames",
        "-show_entries",
        "stream=codec_name,pix_fmt,width,height,nb_read_frames,avg_frame_rate:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LutGradeError(f"ffprobe falló sobre la salida: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise LutGradeError("ffprobe: la salida de la gradación no es legible")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise LutGradeError("ffprobe: salida JSON ilegible") from exc
    streams = data.get("streams") or []
    if not streams:
        raise LutGradeError("ffprobe: la salida no tiene pista de vídeo")
    stream = streams[0]
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    try:
        frame_count = int(stream.get("nb_read_frames") or 0)
    except (TypeError, ValueError):
        frame_count = 0
    return {
        "codec_name": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_count": frame_count,
        "duration_sec": round(duration, 3),
        "has_audio": False,  # la ruta descarta audio: salida video-only
    }


def _run_ffmpeg(
    cmd: list[str], *, timeout_s: float, what: str
) -> None:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise LutGradeError("se requiere ffmpeg en PATH")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LutGradeError(f"ffmpeg falló en {what}: {exc}") from exc
    if result.returncode != 0:
        raise LutGradeError(
            f"ffmpeg falló en {what} (exit {result.returncode}): {result.stderr[-1200:]}"
        )


def _temporal_stability(
    video_path: Path, expected_frames: int, *, timeout_s: float
) -> dict[str, Any]:
    """Métrica temporal sobre pares adyacentes reales decodificados.

    Mide la diferencia RGB media/máxima absoluta entre fotogramas adyacentes
    (≤ ``MAX_STABILITY_PAIRS`` pares muestreados) sobre píxeles decodificados,
    no sobre metadatos.
    """
    if expected_frames < 2:
        raise LutGradeError("la salida no tiene pares adyacentes para medir")
    step = max(1, (expected_frames - 1) // MAX_STABILITY_PAIRS)
    starts = list(range(0, expected_frames - 1, step))[:MAX_STABILITY_PAIRS]
    selected = sorted({index for start in starts for index in (start, start + 1)})
    expression = "+".join(f"eq(n\\,{n})" for n in selected)
    with tempfile.TemporaryDirectory(prefix="comfy_lut_stab_") as tmp:
        out_dir = Path(tmp)
        cmd = [
            find_ffmpeg() or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"select='{expression}'",
            "-fps_mode",
            "passthrough",
            str(out_dir / "stab_%03d.png"),
        ]
        _run_ffmpeg(cmd, timeout_s=timeout_s, what="muestreo de estabilidad temporal")
        paths = sorted(out_dir.glob("stab_*.png"))
        if len(paths) != len(selected):
            raise LutGradeError(
                f"estabilidad: se decodificaron {len(paths)} frames, se esperaban {len(selected)}"
            )
        frames: dict[int, Image.Image] = {}
        for position, path in enumerate(paths):
            index = selected[position]
            try:
                with Image.open(path) as frame:
                    frame.load()
                    frames[index] = frame.convert("RGB").copy()
            except (OSError, Image.DecompressionBombError) as exc:
                raise LutGradeError(
                    f"estabilidad: frame decodificado ilegible ({path.name})"
                ) from exc
        pair_means: list[float] = []
        pair_maxes: list[float] = []
        for start in starts:
            diff = ImageChops.difference(frames[start], frames[start + 1])
            stat = ImageStat.Stat(diff)
            channel_mean = sum(stat.mean) / len(stat.mean)
            channel_max = max(extrema[1] for extrema in stat.extrema)
            pair_means.append(channel_mean)
            pair_maxes.append(channel_max)
            diff.close()
        sampled_pairs = len(starts)
        return {
            "method": "adjacent_frame_rgb_mean_abs_diff",
            "sampled_pairs": sampled_pairs,
            "mean_abs_diff": round(sum(pair_means) / sampled_pairs, 6),
            "max_abs_diff": round(max(pair_maxes), 6),
        }


def _blend_expr(strength: float) -> str:
    s = f"{strength:.6f}"
    return f"all_expr=A*{s}+B*{1.0 - strength:.6f}"


def lut_grade(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    lut_cube_text: Any,
    *,
    strength: Any = DEFAULT_STRENGTH,
    mask_asset_id: str | None = None,
    ffmpeg_timeout_s: float = 60.0,
    ffprobe_timeout_s: float = 20.0,
    max_duration_s: float = MAX_LUT_DURATION_S,
    max_width: int = MAX_LUT_WIDTH,
    max_height: int = MAX_LUT_HEIGHT,
    max_frames: int = MAX_LUT_FRAMES,
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Gradación con LUT 3D CPU acotada sobre un asset del proyecto.

    Devuelve ``(output_bytes, frame_png_bytes, evidence)``. Para vídeo la
    salida es ffv1 bgr0 (lossless) verificada con ``ffprobe``; para imagen es
    un PNG. La evidencia incluye sha256 de fuente, LUT y máscara, probe real
    y la métrica temporal de estabilidad.
    """
    lut_info = parse_lut_cube(lut_cube_text)
    lut_sha256 = hashlib.sha256(str(lut_cube_text).encode("utf-8")).hexdigest()
    strength_value = _validate_strength(strength)
    duration_cap = min(float(max_duration_s), MAX_LUT_DURATION_S)
    width_cap = min(int(max_width), MAX_LUT_WIDTH)
    height_cap = min(int(max_height), MAX_LUT_HEIGHT)
    frames_cap = min(int(max_frames), MAX_LUT_FRAMES)

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise LutGradeError("se requiere ffmpeg y ffprobe en PATH")

    asset = store.get_asset(project_id, asset_id)
    path = store.get_asset_path(project_id, asset_id)
    if asset is None or path is None:
        raise LutGradeError(f"asset inexistente en este proyecto: {asset_id}")
    source_kind = asset.get("kind")
    if source_kind not in {"video", "image"}:
        raise LutGradeError(
            f"{asset_id}: la gradación LUT requiere un asset de vídeo o imagen del proyecto"
        )
    if not path.is_file():
        raise LutGradeError(f"{asset_id}: el archivo del asset no está en disco")
    source_sha256 = _sha256_file(path)

    mask_info: dict[str, Any] | None = None
    mask_path: Path | None = None
    mask_mode: str | None = None
    if mask_asset_id:
        mask_asset = store.get_asset(project_id, mask_asset_id)
        mask_path = store.get_asset_path(project_id, mask_asset_id)
        if mask_asset is None or mask_path is None:
            raise LutGradeError(
                f"máscara {mask_asset_id}: no existe como asset de este proyecto"
            )
        if mask_asset.get("kind") != "image":
            raise LutGradeError(
                f"máscara {mask_asset_id}: debe ser un asset de imagen del proyecto"
            )
        if not mask_path.is_file():
            raise LutGradeError(f"máscara {mask_asset_id}: el archivo no está en disco")

    expected_frames = 0
    source_width = 0
    source_height = 0
    source_probe: dict[str, Any] = {}
    if source_kind == "video":
        source_probe = probe_media_info(path, timeout_s=ffprobe_timeout_s)
        if not source_probe.get("probe_ok") or not source_probe.get("has_video"):
            raise LutGradeError(f"{asset_id}: ffprobe no encontró pista de vídeo legible")
        source_fps = float(source_probe.get("fps") or 0.0)
        source_duration = float(source_probe.get("duration_sec") or 0.0)
        source_width = int(source_probe.get("width") or 0)
        source_height = int(source_probe.get("height") or 0)
        if source_fps <= 0:
            raise LutGradeError(f"{asset_id}: fps no verificable con ffprobe")
        if not 0.0 < source_duration <= duration_cap:
            raise LutGradeError(
                f"{asset_id}: duración {source_duration}s fuera del presupuesto "
                f"(0 < d <= {duration_cap}s)"
            )
        if not (0 < source_width <= width_cap and 0 < source_height <= height_cap):
            raise LutGradeError(
                f"{asset_id}: dimensiones {source_width}x{source_height} fuera del "
                f"presupuesto ({width_cap}x{height_cap})"
            )
        expected_frames = int(round(source_duration * source_fps))
        if expected_frames <= 0 or expected_frames > frames_cap:
            raise LutGradeError(
                f"{asset_id}: {expected_frames} fotogramas fuera del presupuesto "
                f"(1..{frames_cap})"
            )
        if expected_frames * source_width * source_height > MAX_LUT_PIXEL_FRAMES:
            raise LutGradeError(f"{asset_id}: presupuesto de píxeles por secuencia excedido")
    else:
        source_width, source_height = _image_dimensions(path, f"fuente {asset_id}")
        if not (0 < source_width <= width_cap and 0 < source_height <= height_cap):
            raise LutGradeError(
                f"{asset_id}: dimensiones {source_width}x{source_height} fuera del "
                f"presupuesto ({width_cap}x{height_cap})"
            )
        source_probe = {"width": source_width, "height": source_height, "probe_ok": True}

    if mask_path is not None:
        mask_width, mask_height = _image_dimensions(mask_path, f"máscara {mask_asset_id}")
        if (mask_width, mask_height) != (source_width, source_height):
            raise LutGradeError(
                f"máscara {mask_asset_id}: dimensiones {mask_width}x{mask_height} no "
                f"coinciden con la fuente {source_width}x{source_height}"
            )
        mask_mode = _mask_mode(mask_path)
        mask_info = {
            "asset_id": mask_asset_id,
            "sha256": _sha256_file(mask_path),
            "mode": mask_mode,
            "rule": "alpha_if_non_uniform_else_luma",
        }

    is_video = source_kind == "video"
    with tempfile.TemporaryDirectory(prefix="comfy_lut_grade_") as tmp:
        tmp_dir = Path(tmp)
        lut_path = tmp_dir / "grade.cube"
        lut_path.write_text(str(lut_cube_text), encoding="utf-8")
        output_path = tmp_dir / ("lut_grade.mkv" if is_video else "lut_grade.png")
        frame_path = tmp_dir / "frame.png"
        lut_opt = f"lut3d=file={lut_path}"

        if is_video:
            input_args = ["-i", str(path)]
            if mask_path is not None:
                assert mask_asset_id is not None
                input_args = [
                    "-i",
                    str(path),
                    "-loop",
                    "1",
                    "-framerate",
                    f"{float(source_probe['fps']):.6f}",
                    "-i",
                    str(mask_path),
                ]
                # maskedmerge en FFmpeg 9.0.2 solo conserva exactamente los
                # píxeles de la base (mask=0) y aplica el grado (mask=255)
                # cuando base/grado/máscara son planares (gbrp).
                mask_chain = (
                    "alphaextract,format=gbrp"
                    if mask_mode == "alpha"
                    else "format=gray,format=gbrp"
                )
                filter_graph = (
                    f"[0:v]format=rgb24,split=3[b1][b2][b3];"
                    f"[b1]{lut_opt}[g0];"
                    f"[g0][b2]blend={_blend_expr(strength_value)}[graded];"
                    f"[1:v]{mask_chain}[m];"
                    f"[b3]format=gbrp[basep];"
                    f"[graded]format=gbrp[gradedp];"
                    f"[basep][gradedp][m]maskedmerge[vout]"
                )
            else:
                filter_graph = (
                    f"[0:v]format=rgb24,split=2[b1][b2];"
                    f"[b1]{lut_opt}[g0];"
                    f"[g0][b2]blend={_blend_expr(strength_value)}[vout]"
                )
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *input_args,
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                # Codificación determinista: sin metadatos ni timestamps
                # variables, un solo hilo y ffv1 con slices fijos.
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-c:v",
                "ffv1",
                "-level",
                "3",
                "-slices",
                "4",
                "-g",
                "1",
                "-pix_fmt",
                "bgr0",
                "-an",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                str(output_path),
            ]
        else:
            input_args = ["-i", str(path)]
            if mask_path is not None:
                assert mask_asset_id is not None
                input_args = ["-i", str(path), "-i", str(mask_path)]
                mask_chain = (
                    "format=rgba,alphaextract,format=gbrp"
                    if mask_mode == "alpha"
                    else "format=gray,format=gbrp"
                )
                filter_graph = (
                    f"[0:v]format=rgb24,split=3[b1][b2][b3];"
                    f"[b1]{lut_opt}[g0];"
                    f"[g0][b2]blend={_blend_expr(strength_value)}[graded];"
                    f"[1:v]{mask_chain}[m];"
                    f"[b3]format=gbrp[basep];"
                    f"[graded]format=gbrp[gradedp];"
                    f"[basep][gradedp][m]maskedmerge[vout]"
                )
            else:
                filter_graph = (
                    f"[0:v]format=rgb24,split=2[b1][b2];"
                    f"[b1]{lut_opt}[g0];"
                    f"[g0][b2]blend={_blend_expr(strength_value)}[vout]"
                )
            cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                *input_args,
                "-filter_complex",
                filter_graph,
                "-map",
                "[vout]",
                "-map_metadata",
                "-1",
                "-fflags",
                "+bitexact",
                "-flags:v",
                "+bitexact",
                "-frames:v",
                "1",
                "-update",
                "1",
                "-pix_fmt",
                "rgb24",
                "-threads",
                "1",
                "-filter_threads",
                "1",
                "-filter_complex_threads",
                "1",
                str(output_path),
            ]
        _run_ffmpeg(cmd, timeout_s=ffmpeg_timeout_s, what="gradación LUT")

        if is_video:
            output_probe = _probe_output(output_path, timeout_s=ffprobe_timeout_s)
            if output_probe["codec_name"] != "ffv1":
                raise LutGradeError("ffprobe: la salida no es ffv1 lossless")
            if output_probe["width"] != source_width or output_probe["height"] != source_height:
                raise LutGradeError("ffprobe: dimensiones de salida no coinciden con la fuente")
            if output_probe["frame_count"] != expected_frames:
                raise LutGradeError(
                    f"ffprobe: {output_probe['frame_count']} frames de salida != "
                    f"{expected_frames} esperados de la fuente"
                )
            output_probe["fps"] = round(float(source_probe.get("fps") or 0.0), 6)
            output_probe["container"] = "matroska"
            frame_cmd = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(output_path),
                "-frames:v",
                "1",
                "-update",
                "1",
                str(frame_path),
            ]
            _run_ffmpeg(frame_cmd, timeout_s=ffprobe_timeout_s, what="extracción de frame")
            stability = _temporal_stability(
                output_path, expected_frames, timeout_s=ffprobe_timeout_s
            )
        else:
            output_probe = None
            stability = None
            frame_path.write_bytes(output_path.read_bytes())

        output_bytes = output_path.read_bytes()
        frame_png = frame_path.read_bytes()

    evidence: dict[str, Any] = {
        "renderer": "ffmpeg-lut3d-cpu",
        "style": "user_directed_lut_grade",
        "neural_style_transfer": "not_claimed",
        "model_inference": "none",
        "strength": strength_value,
        "lut": {
            "grid": lut_info["grid"],
            "lines": lut_info["lines"],
            "size_bytes": lut_info["size_bytes"],
            "sha256": lut_sha256,
        },
        "source": {
            "asset_id": asset_id,
            "kind": source_kind,
            "sha256": source_sha256,
            "probe": source_probe,
        },
        "output": {"sha256": hashlib.sha256(output_bytes).hexdigest()},
    }
    if mask_info is not None:
        evidence["mask"] = mask_info
    if output_probe is not None:
        evidence["output_probe"] = output_probe
    if stability is not None:
        evidence["temporal_stability"] = stability
    return output_bytes, frame_png, evidence


__all__ = [
    "DEFAULT_STRENGTH",
    "LutGradeError",
    "MAX_LUT_BYTES",
    "MAX_LUT_DURATION_S",
    "MAX_LUT_GRID",
    "lut_grade",
    "parse_lut_cube",
]
