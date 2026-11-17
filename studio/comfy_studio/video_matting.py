"""Bounded CPU chroma-key matting para vídeo del proyecto (C29/A17).

Ruta explícita y acotada: el usuario elige UN color plano de fondo y ese color
se vuelve transparente en cada fotograma. La salida es una secuencia de vídeo
con canal alpha (ffv1 rgba/bgra, lossless) verificada con ``ffprobe``, más un
fotograma RGBA de referencia. Esto NO es matting semántico ni matting general:
no hay inferencia de modelo y no se reclama calidad de modelo alguno.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from comfy_studio.media_bins import find_ffmpeg, find_ffprobe
from comfy_studio.perception.keyframes import probe_media_info
from comfy_studio.projects import ProjectStore


class ChromaKeyError(ValueError):
    """El chroma-key no puede ejecutarse de forma segura sobre la entrada."""


#: Presupuesto de trabajo: la ruta es CPU y debe terminar siempre.
MAX_CHROMA_DURATION_S = 30.0
MAX_CHROMA_WIDTH = 1920
MAX_CHROMA_HEIGHT = 1080
MAX_CHROMA_FRAMES = 900
MAX_CHROMA_PIXEL_FRAMES = 250_000_000
MAX_COVERAGE_SAMPLE_FRAMES = 16

DEFAULT_SIMILARITY = 0.18
DEFAULT_BLEND = 0.10


def parse_key_color(value: Any) -> str:
    """Normaliza un color plano explícito a ``0xRRGGBB``.

    Acepta ``#RRGGBB``, ``RRGGBB``, ``0xRRGGBB`` o una lista ``[r, g, b]``.
    """
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise ChromaKeyError("key_color debe tener exactamente 3 componentes RGB")
        try:
            channels = [int(item) for item in value]
        except (TypeError, ValueError) as exc:
            raise ChromaKeyError("key_color RGB debe contener enteros") from exc
        if any(not 0 <= channel <= 255 for channel in channels):
            raise ChromaKeyError("cada componente de key_color debe estar en [0, 255]")
        return "0x{:02X}{:02X}{:02X}".format(*channels)
    if not isinstance(value, str):
        raise ChromaKeyError("key_color debe ser '#RRGGBB' o [r, g, b]")
    text = value.strip().lstrip("#")
    if text.lower().startswith("0x"):
        text = text[2:]
    if len(text) != 6 or any(char not in "0123456789abcdefABCDEF" for char in text):
        raise ChromaKeyError("key_color debe ser '#RRGGBB' o [r, g, b]")
    return "0x" + text.upper()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_range(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ChromaKeyError(f"{name} debe ser un número") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ChromaKeyError(f"{name} debe estar en [0, 1]")
    return number


def _probe_output(path: Path, *, timeout_s: float) -> dict[str, Any]:
    """ffprobe directo del resultado: códec, pix_fmt alpha, fps y nº de frames."""
    ffprobe = find_ffprobe()
    if not ffprobe:
        raise ChromaKeyError("se requiere ffprobe en PATH")
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
        raise ChromaKeyError(f"ffprobe falló sobre la salida: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ChromaKeyError("ffprobe: la salida del chroma-key no es legible")
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        raise ChromaKeyError("ffprobe: salida JSON ilegible") from exc
    streams = data.get("streams") or []
    if not streams:
        raise ChromaKeyError("ffprobe: la salida no tiene pista de vídeo")
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
    avg_rate = str(stream.get("avg_frame_rate") or "")
    fps = 0.0
    if "/" in avg_rate:
        try:
            numerator, denominator = avg_rate.split("/", 1)
            if float(denominator) != 0:
                fps = round(float(numerator) / float(denominator), 6)
        except (TypeError, ValueError):
            fps = 0.0
    return {
        "codec_name": stream.get("codec_name"),
        "pix_fmt": stream.get("pix_fmt"),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frame_count": frame_count,
        "fps": fps,
        "duration_sec": round(duration, 3),
        "has_audio": False,  # la ruta descarta audio: matted video-only
    }


def _coverage_stats(
    video_path: Path,
    expected_frames: int,
    *,
    timeout_s: float,
) -> tuple[dict[str, Any], bytes]:
    """Decodifica ≤ MAX_COVERAGE_SAMPLE_FRAMES frames reales y mide alpha.

    Devuelve (estadísticas de cobertura, PNG RGBA del primer frame muestreado).
    La medición se hace sobre píxeles decodificados, no sobre metadatos.
    """
    if expected_frames <= 0:
        raise ChromaKeyError("la salida no tiene fotogramas")
    step = max(1, expected_frames // MAX_COVERAGE_SAMPLE_FRAMES)
    selected: list[int] = []
    index = 0
    while index < expected_frames and len(selected) < MAX_COVERAGE_SAMPLE_FRAMES:
        selected.append(index)
        index += step
    if selected[-1] != expected_frames - 1 and expected_frames > 1:
        # Garantiza un muestreo del final de la secuencia.
        selected.append(expected_frames - 1)
    expression = "+".join(f"eq(n\\,{n})" for n in selected)
    with tempfile.TemporaryDirectory(prefix="comfy_chroma_cov_") as tmp:
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
            str(out_dir / "cov_%03d.png"),
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChromaKeyError(f"ffmpeg falló al muestrear cobertura: {exc}") from exc
        if proc.returncode != 0:
            raise ChromaKeyError(f"ffmpeg: muestreo de cobertura falló: {proc.stderr[-500:]}")
        paths = sorted(out_dir.glob("cov_*.png"))
        if not paths:
            raise ChromaKeyError("cobertura: no se pudo decodificar ningún frame")
        keyed_fracs: list[float] = []
        opaque_fracs: list[float] = []
        first_frame_png = b""
        for position, path in enumerate(paths):
            try:
                with Image.open(path) as frame:
                    frame.load()
                    rgba = frame.convert("RGBA")
            except (OSError, Image.DecompressionBombError) as exc:
                raise ChromaKeyError(
                    f"cobertura: frame decodificado ilegible ({path.name})"
                ) from exc
            alpha = rgba.getchannel("A")
            histogram = alpha.histogram()
            total = rgba.width * rgba.height
            keyed = sum(histogram[:128])
            opaque = sum(histogram[254:])
            keyed_fracs.append(keyed / total)
            opaque_fracs.append(opaque / total)
            if position == 0:
                buffer = io.BytesIO()
                rgba.save(buffer, format="PNG", optimize=False)
                first_frame_png = buffer.getvalue()
            rgba.close()
    sampled = len(keyed_fracs)
    return {
        "sampled_frames": sampled,
        "keyed_mean": round(sum(keyed_fracs) / sampled, 6),
        "keyed_min": round(min(keyed_fracs), 6),
        "keyed_max": round(max(keyed_fracs), 6),
        "opaque_mean": round(sum(opaque_fracs) / sampled, 6),
        "opaque_min": round(min(opaque_fracs), 6),
        "opaque_max": round(max(opaque_fracs), 6),
    }, first_frame_png


def chroma_key_video(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    key_color: Any,
    *,
    similarity: Any = DEFAULT_SIMILARITY,
    blend: Any = DEFAULT_BLEND,
    ffmpeg_timeout_s: float = 60.0,
    ffprobe_timeout_s: float = 20.0,
    max_duration_s: float = MAX_CHROMA_DURATION_S,
    max_width: int = MAX_CHROMA_WIDTH,
    max_height: int = MAX_CHROMA_HEIGHT,
    max_frames: int = MAX_CHROMA_FRAMES,
) -> tuple[bytes, bytes, dict[str, Any]]:
    """Chroma-key CPU acotado sobre un vídeo del proyecto.

    Devuelve ``(video_alpha_bytes, frame_png_bytes, evidence)``. El vídeo de
    salida es ffv1 rgba/bgra (lossless, portador de alpha) verificado con
    ``ffprobe``; la evidencia incluye sha256 de la fuente y cobertura real.
    """
    color = parse_key_color(key_color)
    sim = _validate_range("similarity", similarity)
    blend_value = _validate_range("blend", blend)
    duration_cap = min(float(max_duration_s), MAX_CHROMA_DURATION_S)
    width_cap = min(int(max_width), MAX_CHROMA_WIDTH)
    height_cap = min(int(max_height), MAX_CHROMA_HEIGHT)
    frames_cap = min(int(max_frames), MAX_CHROMA_FRAMES)

    ffmpeg = find_ffmpeg()
    ffprobe = find_ffprobe()
    if not ffmpeg or not ffprobe:
        raise ChromaKeyError("se requiere ffmpeg y ffprobe en PATH")

    asset = store.get_asset(project_id, asset_id)
    path = store.get_asset_path(project_id, asset_id)
    if asset is None or path is None:
        raise ChromaKeyError(f"asset de vídeo inexistente en este proyecto: {asset_id}")
    if asset.get("kind") != "video":
        raise ChromaKeyError(f"{asset_id}: el chroma-key requiere un asset de vídeo del proyecto")
    if not path.is_file():
        raise ChromaKeyError(f"{asset_id}: el archivo de vídeo no está en disco")

    source_sha256 = _sha256_file(path)
    source_probe = probe_media_info(path, timeout_s=ffprobe_timeout_s)
    if not source_probe.get("probe_ok") or not source_probe.get("has_video"):
        raise ChromaKeyError(f"{asset_id}: ffprobe no encontró pista de vídeo legible")
    source_fps = float(source_probe.get("fps") or 0.0)
    source_duration = float(source_probe.get("duration_sec") or 0.0)
    source_width = int(source_probe.get("width") or 0)
    source_height = int(source_probe.get("height") or 0)
    if source_fps <= 0:
        raise ChromaKeyError(f"{asset_id}: fps no verificable con ffprobe")
    if not 0.0 < source_duration <= duration_cap:
        raise ChromaKeyError(
            f"{asset_id}: duración {source_duration}s fuera del presupuesto "
            f"(0 < d <= {duration_cap}s)"
        )
    if not (0 < source_width <= width_cap and 0 < source_height <= height_cap):
        raise ChromaKeyError(
            f"{asset_id}: dimensiones {source_width}x{source_height} fuera del "
            f"presupuesto ({width_cap}x{height_cap})"
        )
    expected_frames = int(round(source_duration * source_fps))
    if expected_frames <= 0 or expected_frames > frames_cap:
        raise ChromaKeyError(
            f"{asset_id}: {expected_frames} fotogramas fuera del presupuesto (1..{frames_cap})"
        )
    if expected_frames * source_width * source_height > MAX_CHROMA_PIXEL_FRAMES:
        raise ChromaKeyError(f"{asset_id}: presupuesto de píxeles por secuencia excedido")

    filter_graph = f"colorkey={color}:{sim:.6f}:{blend_value:.6f},format=rgba"
    with tempfile.TemporaryDirectory(prefix="comfy_chroma_") as tmp:
        output_path = Path(tmp) / "chroma_key_alpha.mkv"
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(path),
            "-vf",
            filter_graph,
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-pix_fmt",
            "bgra",
            "-an",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=ffmpeg_timeout_s, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ChromaKeyError(f"ffmpeg falló: {exc}") from exc
        if result.returncode != 0:
            raise ChromaKeyError(
                f"ffmpeg falló (exit {result.returncode}): {result.stderr[-1200:]}"
            )
        output_probe = _probe_output(output_path, timeout_s=ffprobe_timeout_s)
        if output_probe["codec_name"] != "ffv1":
            raise ChromaKeyError("ffprobe: la salida no es ffv1 lossless")
        if output_probe["pix_fmt"] not in {"rgba", "bgra"}:
            raise ChromaKeyError(
                f"ffprobe: la salida no porta alpha (pix_fmt={output_probe['pix_fmt']})"
            )
        if output_probe["width"] != source_width or output_probe["height"] != source_height:
            raise ChromaKeyError("ffprobe: dimensiones de salida no coinciden con la fuente")
        if output_probe["frame_count"] != expected_frames:
            raise ChromaKeyError(
                f"ffprobe: {output_probe['frame_count']} frames de salida != "
                f"{expected_frames} esperados de la fuente"
            )
        coverage, frame_png = _coverage_stats(
            output_path, expected_frames, timeout_s=ffprobe_timeout_s
        )
        video_bytes = output_path.read_bytes()

    output_probe["container"] = "matroska"
    evidence = {
        "renderer": "ffmpeg-colorkey-cpu",
        "matting": "chroma_key_flat_color",
        "matting_scope": "explicit_user_selected_flat_background_color",
        "semantic_matting": "not_claimed",
        "general_matting": "not_performed",
        "model_inference": "none",
        "key_color": color.lower(),
        "key_color_rgb": [
            int(color[2:4], 16),
            int(color[4:6], 16),
            int(color[6:8], 16),
        ],
        "similarity": sim,
        "blend": blend_value,
        "source": {
            "asset_id": asset_id,
            "kind": "video",
            "sha256": source_sha256,
            "probe": source_probe,
        },
        "output_probe": output_probe,
        "coverage": coverage,
    }
    return video_bytes, frame_png, evidence


__all__ = [
    "ChromaKeyError",
    "DEFAULT_BLEND",
    "DEFAULT_SIMILARITY",
    "MAX_CHROMA_DURATION_S",
    "chroma_key_video",
    "parse_key_color",
]
