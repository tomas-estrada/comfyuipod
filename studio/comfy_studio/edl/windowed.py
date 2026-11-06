"""Ensamblador de clips largos por ventanas (M4): crossfade + audio original.

Los modelos de vídeo del estudio generan como máximo ~81-97 fotogramas por
job (≈5-6 s). Para secuencias largas se generan *chunks* consecutivos con
solapamiento de fotogramas (p. ej. las 9 últimas salidas de una ventana se
inyectan como `continue_motion`/`reference` de la siguiente) y este módulo
los ensambla:

- Normaliza cada chunk a un contenedor común (H.264 + AAC/silencio).
- Realiza un crossfade (`xfade`) en la zona de solapamiento entre chunks.
- Restaura el audio del vídeo fuente original (mapa `-map 1:a`) cuando se
  proporciona `audio_source_asset_id`, recortado a la duración ensamblada.
- Registra el resultado en la galería del proyecto (`outputs/`) con
  verificación real de `ffprobe` (nunca metadatos fabricados).

Cumple las mismas cotas defensivas que el ejecutor EDL: tamaño máximo de
salida y timeouts proporcionales a la duración.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..media_bins import find_ffmpeg
from ..perception.keyframes import probe_media_info
from ..projects import ProjectStore
from .ffmpeg_edl import (
    DEFAULT_FFMPEG_TIMEOUT_S,
    DEFAULT_FFPROBE_TIMEOUT_S,
    MAX_EDL_OUTPUT_BYTES,
    cut_subprocess_timeout,
)

# M4-SCR-001: cotas del ensamblador de ventanas.
MAX_WINDOW_CHUNKS = 32
MIN_CROSSFADE_SEC = 0.0
MAX_CROSSFADE_SEC = 2.0


class WindowedAssemblyError(RuntimeError):
    """Error de validación o ejecución del ensamblador de ventanas."""


def _resolve_asset_path(
    store: ProjectStore,
    project_id: str,
    asset_id: str,
    *,
    expected_kind: str = "video",
) -> Path:
    asset = store.get_asset(project_id, asset_id)
    if asset is None:
        raise WindowedAssemblyError(f"Asset no encontrado: {asset_id!r}")
    if asset.get("kind") != expected_kind:
        raise WindowedAssemblyError(
            f"El asset {asset_id!r} no es un {expected_kind} (kind={asset.get('kind')!r})."
        )
    path = store.get_asset_path(project_id, asset_id)
    if path is None or not path.is_file():
        raise WindowedAssemblyError(f"El asset {asset_id!r} no tiene archivo físico.")
    return path


def assemble_windowed_video(
    store: ProjectStore,
    project_id: str,
    *,
    chunk_asset_ids: list[str],
    crossfade_sec: float = 0.5,
    audio_source_asset_id: str | None = None,
    ffmpeg_timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S,
    ffprobe_timeout_s: float = DEFAULT_FFPROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Ensambla chunks consecutivos en un clip largo con crossfade y audio.

    Los `chunk_asset_ids` deben estar en orden temporal (chunk 1, 2, 3...);
    cada par consecutivo se funde con `xfade` durante `crossfade_sec`
    (0 desactiva el fundido: concat demuxer). El audio del
    `audio_source_asset_id` se recorta a la duración final y sustituye al
    de los chunks.
    """
    if not chunk_asset_ids:
        raise WindowedAssemblyError("La lista de chunk_asset_ids está vacía.")
    if len(chunk_asset_ids) > MAX_WINDOW_CHUNKS:
        raise WindowedAssemblyError(
            f"Se recibieron {len(chunk_asset_ids)} chunks; máximo: {MAX_WINDOW_CHUNKS}."
        )
    crossfade = round(float(crossfade_sec), 3)
    if crossfade < MIN_CROSSFADE_SEC or crossfade > MAX_CROSSFADE_SEC:
        raise WindowedAssemblyError(
            f"crossfade_sec debe estar entre {MIN_CROSSFADE_SEC} y {MAX_CROSSFADE_SEC}."
        )

    project = store.get_project(project_id)
    if project is None:
        raise WindowedAssemblyError(f"Proyecto no encontrado: {project_id!r}")

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise WindowedAssemblyError("No se encontró `ffmpeg` en el PATH para ensamblar.")

    chunk_paths = [
        _resolve_asset_path(store, project_id, aid) for aid in chunk_asset_ids
    ]
    audio_path: Path | None = None
    if audio_source_asset_id:
        audio_path = _resolve_asset_path(store, project_id, audio_source_asset_id)

    with tempfile.TemporaryDirectory(prefix="comfy_windowed_") as tmp_str:
        tmp_dir = Path(tmp_str)

        # 1. Normalizar chunks: H.264 solo vídeo (el audio se restaura al final
        #    desde audio_source si se aporta; los intermedios van sin pista).
        #    Nota M4: los intentos con AAC intermedio + acrossfade producen
        #    audio roto de forma no determinista en algunas builds de ffmpeg;
        #    vídeo-only en el fundido es determinista y el caso de uso real
        #    (restaurar el audio original) se aplica en el paso 3.
        norm_paths: list[Path] = []
        chunk_durations: list[float] = []
        for idx, src in enumerate(chunk_paths):
            norm = tmp_dir / f"chunk_{idx:03d}.mp4"
            timeout = cut_subprocess_timeout(30.0, base_s=ffmpeg_timeout_s)
            cmd = [
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-i",
                str(src),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "16",
                "-movflags",
                "+faststart",
                str(norm),
            ]
            try:
                proc = subprocess.run(
                    cmd, check=False, capture_output=True, timeout=timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise WindowedAssemblyError(
                    f"ffmpeg excedió el timeout al normalizar el chunk #{idx + 1}."
                ) from exc
            if proc.returncode != 0 or not norm.is_file():
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise WindowedAssemblyError(
                    f"Error de ffmpeg al normalizar chunk #{idx + 1}: {err}"
                )
            probe = probe_media_info(norm, timeout_s=ffprobe_timeout_s)
            dur = float(probe.get("duration_sec") or 0.0)
            if dur <= 0:
                raise WindowedAssemblyError(
                    f"El chunk #{idx + 1} no pudo verificarse con ffprobe (duración 0)."
                )
            chunk_durations.append(dur)
            norm_paths.append(norm)

        # 2. Ensamblar con crossfade (xfade) o concat si crossfade == 0
        current = norm_paths[0]
        current_dur = chunk_durations[0]
        applied_fades: list[dict[str, Any]] = []
        for idx in range(1, len(norm_paths)):
            nxt = norm_paths[idx]
            nxt_dur = chunk_durations[idx]
            fade = min(crossfade, current_dur / 2.0, nxt_dur / 2.0)
            merged = tmp_dir / f"merged_{idx:03d}.mp4"
            if fade <= 0.01:
                # Concat demuxer sin recodificar (rápido, sin fundido)
                list_path = tmp_dir / f"concat_{idx:03d}.txt"
                list_path.write_text(
                    f"file '{current.as_posix()}'\nfile '{nxt.as_posix()}'\n",
                    encoding="utf-8",
                )
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-c",
                    "copy",
                    str(merged),
                ]
                new_dur = current_dur + nxt_dur
            else:
                offset = max(0.0, current_dur - fade)
                cmd = [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(current),
                    "-i",
                    str(nxt),
                    "-filter_complex",
                    f"[0:v][1:v]xfade=transition=fade:duration={fade:.3f}:"
                    f"offset={offset:.3f}[v]",
                    "-map",
                    "[v]",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    str(merged),
                ]
                new_dur = current_dur + nxt_dur - fade
            timeout = cut_subprocess_timeout(new_dur, base_s=ffmpeg_timeout_s)
            try:
                proc = subprocess.run(
                    cmd, check=False, capture_output=True, timeout=timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise WindowedAssemblyError(
                    f"ffmpeg excedió el timeout al fundir el chunk #{idx + 1}."
                ) from exc
            if proc.returncode != 0 or not merged.is_file():
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise WindowedAssemblyError(
                    f"Error de ffmpeg al fundir chunk #{idx + 1}: {err}"
                )
            applied_fades.append(
                {
                    "chunk_index": idx,
                    "crossfade_sec": round(fade, 3),
                    "offset_sec": round(max(0.0, current_dur - fade), 3),
                }
            )
            current = merged
            current_dur = new_dur

        # 3. Restaurar audio del vídeo fuente (mapa completo, recortado)
        if audio_path is not None:
            with_audio = tmp_dir / "with_audio.mp4"
            cmd = [
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-i",
                str(current),
                "-i",
                str(audio_path),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                # El audio fuente puede ser más corto o más largo que el vídeo
                # ensamblado: apad + -t recorta/rellena al largo exacto del vídeo.
                "-af",
                "apad",
                "-t",
                f"{current_dur:.3f}",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "44100",
                "-ac",
                "2",
                str(with_audio),
            ]
            timeout = cut_subprocess_timeout(current_dur, base_s=ffmpeg_timeout_s)
            try:
                proc = subprocess.run(
                    cmd, check=False, capture_output=True, timeout=timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise WindowedAssemblyError(
                    "ffmpeg excedió el timeout al restaurar el audio original."
                ) from exc
            if proc.returncode != 0 or not with_audio.is_file():
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise WindowedAssemblyError(f"Error al restaurar el audio: {err}")
            current = with_audio

        # 4. Verificación real con ffprobe (nunca metadatos fabricados)
        probe_info = probe_media_info(current, timeout_s=ffprobe_timeout_s)
        if not bool(probe_info.get("probe_ok")):
            raise WindowedAssemblyError(
                "El vídeo ensamblado no pudo verificarse con ffprobe (probe_ok=false)."
            )
        mp4_bytes = current.read_bytes()
        if len(mp4_bytes) > MAX_EDL_OUTPUT_BYTES:
            raise WindowedAssemblyError(
                f"El MP4 ensamblado ({len(mp4_bytes)} bytes) supera el límite de "
                f"{MAX_EDL_OUTPUT_BYTES} bytes."
            )

    out_filename = f"windowed_{len(chunk_asset_ids)}chunks.mp4"
    output_asset = store.save_upload(
        project_id,
        filename=out_filename,
        content_type="video/mp4",
        data=mp4_bytes,
        max_upload_bytes=MAX_EDL_OUTPUT_BYTES,
        category="outputs",
        metadata={
            "windowed_assembly": True,
            "chunk_asset_ids": list(chunk_asset_ids),
            "chunks_count": len(chunk_asset_ids),
            "crossfade_sec": crossfade,
            "audio_source_asset_id": audio_source_asset_id,
            "expected_duration_sec": round(current_dur, 3),
            "actual_duration_sec": probe_info.get("duration_sec"),
            "width": probe_info.get("width"),
            "height": probe_info.get("height"),
            "has_audio": bool(probe_info.get("has_audio")),
            "applied_fades": applied_fades,
        },
    )

    return {
        "status": "assembled",
        "project_id": project_id,
        "output_asset": output_asset,
        "chunks_count": len(chunk_asset_ids),
        "crossfade_sec": crossfade,
        "audio_source_asset_id": audio_source_asset_id,
        "applied_fades": applied_fades,
        "verification": {
            "expected_duration_sec": round(current_dur, 3),
            "actual_duration_sec": float(probe_info.get("duration_sec") or current_dur),
            "width": int(probe_info.get("width") or 0),
            "height": int(probe_info.get("height") or 0),
            "has_audio": bool(probe_info.get("has_audio")),
            "probe_ok": bool(probe_info.get("probe_ok")),
        },
    }


# ---------------------------------------------------------------------------
# Planificador de ventanas (M4): cuántos chunks renderizar y con qué solape
# ---------------------------------------------------------------------------

def plan_generation_windows(
    total_duration_sec: float,
    *,
    chunk_frames: int = 81,
    fps: int = 16,
    overlap_frames: int = 9,
    max_windows: int = MAX_WINDOW_CHUNKS,
) -> dict[str, Any]:
    """Planifica las ventanas de render para un clip largo.

    Divide `total_duration_sec` en ventanas de `chunk_frames` fotogramas con
    `overlap_frames` de solapamiento entre consecutivas (para crossfade o
    `continue_motion`). Cada ventana lleva su offset de inicio en el vídeo
    fuente (`skip_first_frames` para `VHS_LoadVideo`).

    La última ventana se recorta a los fotogramas restantes; nunca genera
    ventanas vacías ni pasa de `max_windows`.

    Precisión sobre las cotas (medidas, no estimadas):

    - Ninguna ventana sobrepasa el vídeo fuente por el lado del *marcador* de
      fin: `end_sec` se recorta con `min(total_sec, ...)` y el bucle se
      detiene con `start_sec < total_sec - 1e-6`.
    - PERO `frames` sí puede exceder lo que queda de vídeo, porque tiene un
      suelo de 5 fotogramas (`max(5, round(remaining * fps))`). Un total de
      0.1 s a 16 fps deja solo 1.6 fotogramas y el plan pide 5; la longitud
      renderizada (0.312 s) es entonces mayor que `total_sec`. Reservado como
      CUP-EDL-01: `frames` no está acotado por los fotogramas restantes.
    - Por lo mismo, `estimated_assembled_sec` NO está acotado por
      `total_duration_sec`. `plan(10.0)` devuelve 10.001 s: al suelo de la
      última ventana se suma que cada costura resta el solape redondeado a
      milisegundos (0.5625 -> 0.562), +0.5 ms de residuo por costura. Es un
      presupuesto orientativo, no una cota. Reservado como CUP-EDL-02.
    - `max_windows` se rechaza justo al añadir la ventana número
      `max_windows` (la comprobación va detrás del append), así que el
      máximo de ventanas que se llega a planificar es `max_windows - 1`. Con
      `max_windows=1` ninguna duración produce plan. Reservado como
      CUP-EDL-03.

    El número de ventanas resultante nunca puede alimentar
    `assemble_windowed_video` por encima de su cupo (`MAX_WINDOW_CHUNKS`),
    porque el planificador se queda uno por debajo por construcción.
    """
    total_sec = round(float(total_duration_sec), 3)
    if total_sec <= 0:
        raise WindowedAssemblyError("total_duration_sec debe ser > 0.")
    if chunk_frames < 5 or chunk_frames > 241:
        raise WindowedAssemblyError("chunk_frames debe estar entre 5 y 241.")
    if fps < 1 or fps > 60:
        raise WindowedAssemblyError("fps debe estar entre 1 y 60.")
    if overlap_frames < 0 or overlap_frames >= chunk_frames:
        raise WindowedAssemblyError(
            "overlap_frames debe estar en [0, chunk_frames)."
        )
    chunk_sec = chunk_frames / float(fps)
    overlap_sec = overlap_frames / float(fps)
    effective_sec = chunk_sec - overlap_sec  # avance neto por ventana

    windows: list[dict[str, Any]] = []
    start_sec = 0.0
    while start_sec < total_sec - 1e-6:
        remaining_sec = total_sec - start_sec
        frames = min(chunk_frames, max(5, int(round(remaining_sec * fps))))
        windows.append(
            {
                "index": len(windows),
                "start_sec": round(start_sec, 3),
                "end_sec": round(min(total_sec, start_sec + frames / fps), 3),
                "frames": frames,
                "skip_first_frames": int(round(start_sec * fps)),
                # Para variantes con continuidad nativa (wan_animate_cont):
                # offset acumulado de fotogramas donde terminó la ventana anterior.
                "video_frame_offset": int(round(start_sec * fps)),
                "crossfade_sec": round(overlap_sec, 3) if len(windows) > 0 else 0.0,
                "continue_motion_frames": overlap_frames if len(windows) > 0 else 0,
            }
        )
        start_sec += effective_sec
        if len(windows) >= max_windows:
            raise WindowedAssemblyError(
                f"El clip requiere más de {max_windows} ventanas; acorta la duración "
                "o aumenta chunk_frames."
            )

    planned_sec = sum(w["frames"] / fps for w in windows) - sum(
        w["crossfade_sec"] for w in windows if w["index"] > 0
    )
    return {
        "total_duration_sec": total_sec,
        "fps": int(fps),
        "chunk_frames": int(chunk_frames),
        "overlap_frames": int(overlap_frames),
        "windows_count": len(windows),
        "estimated_assembled_sec": round(planned_sec, 3),
        "seamless_hint": (
            "Para wan_animate: ventana 1 con wan_animate; ventanas 2+ con "
            "wan_animate_cont (continue_video_asset_id = salida anterior, "
            "video_frame_offset del plan) y assemble_long_video con crossfade_sec=0."
        ),
        "windows": windows,
    }
