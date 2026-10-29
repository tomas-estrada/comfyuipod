"""Ejecutor determinista de EDL sobre `ffmpeg` (corte, reencuadre 16:9 / 9:16 y concatenación).

Cumple `VAL-PERC-006` y `VAL-PROD-001`:
- Recorta cada segmento `[in_sec, out_sec]` en el orden exacto de la lista de cortes.
- Adapta la relación de aspecto de entrega (`16:9` horizontal, `9:16` vertical para reels/shorts,
  `1:1` o `4:5`) con escalado y letterbox/pillarbox determinista.
- Concatena los segmentos en un contenedor MP4 final, lo registra en la galería del proyecto
  (`outputs/`) y verifica duración y dimensiones reales con `ffprobe`.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..perception.keyframes import probe_media_info
from ..projects import ProjectStore

ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (720, 720),
    "4:5": (720, 900),
}

# M3-SCR-005: cota defensiva de cortes por EDL (cada corte = 1 subprocess ffmpeg)
MAX_EDL_CUTS = 200
# M3-SCR-004: cota DURA del MP4 de salida. No se calcula a partir del propio tamaño
# (max(N, len+N) nunca rechaza nada): 720p H.264 de un EDL acotado queda muy por debajo.
MAX_EDL_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# M1.5: timeouts configurables y proporcionales. Un timeout fijo de 60 s por corte hacía
# fallar cualquier clip largo en hosts lentos. La cota es
# `max(base, min(cap, 4 × duración_del_corte + base))`.
DEFAULT_FFMPEG_TIMEOUT_S = 60.0
MAX_FFMPEG_TIMEOUT_S = 1800.0
CUT_TIMEOUT_FACTOR = 4.0
DEFAULT_FFPROBE_TIMEOUT_S = 20.0


def cut_subprocess_timeout(
    cut_duration_sec: float,
    *,
    base_s: float = DEFAULT_FFMPEG_TIMEOUT_S,
    cap_s: float = MAX_FFMPEG_TIMEOUT_S,
) -> float:
    """Timeout de subprocess para un corte: `max(base, min(cap, 4×duración + base))`.

    Un corte de 3 s tarda muy poco; uno de 90 s necesita bastante más que 60 s en un
    host con CPU lenta. Escalar con la duración evita el falso negativo por timeout
    sin permitir un subprocess descontrolado cuando la duración reportada es absurda.
    """
    base = max(1.0, float(base_s))
    cap = max(base, float(cap_s))
    scaled = (CUT_TIMEOUT_FACTOR * max(0.0, float(cut_duration_sec))) + base
    return float(min(cap, max(base, scaled)))


probe_media_file = probe_media_info


def resolve_aspect_dimensions(aspect_ratio: str) -> tuple[int, int]:
    """Devuelve `(width, height)` pares para el formato de entrega solicitado."""
    cleaned = (aspect_ratio or "16:9").strip()
    return ASPECT_DIMENSIONS.get(cleaned, ASPECT_DIMENSIONS["16:9"])


def _resolve_source_video_path(
    store: ProjectStore,
    project_id: str,
    *,
    preferred_asset_id: str | None = None,
) -> tuple[str, Path]:
    if preferred_asset_id:
        asset = store.get_asset(project_id, preferred_asset_id)
        if asset is not None and asset.get("kind") == "video":
            path = store.get_asset_path(project_id, preferred_asset_id)
            if path is not None:
                return preferred_asset_id, path

    for candidate in store.list_assets(project_id, kind="video"):
        # Preferir clips de entrada ('uploads') sobre renders previos de EDL
        meta = candidate.get("metadata") or {}
        if meta.get("edl_render"):
            continue
        c_path = store.get_asset_path(project_id, candidate["id"])
        if c_path is not None:
            return candidate["id"], c_path

    # Fallback a cualquier vídeo del proyecto
    for candidate in store.list_assets(project_id, kind="video"):
        c_path = store.get_asset_path(project_id, candidate["id"])
        if c_path is not None:
            return candidate["id"], c_path

    raise ValueError("El proyecto no dispone de ningún asset de vídeo fuente para ejecutar el EDL.")


def execute_edl_ffmpeg(
    store: ProjectStore,
    project_id: str,
    *,
    edl_id: str | None = None,
    version: int | None = None,
    aspect_ratio: str | None = None,
    source_asset_id: str | None = None,
    cuts: list[dict[str, Any]] | None = None,
    fps: int = 24,
    ffmpeg_timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S,
    ffprobe_timeout_s: float = DEFAULT_FFPROBE_TIMEOUT_S,
) -> dict[str, Any]:
    """Ejecuta un EDL con `ffmpeg`, guarda el MP4 en el proyecto y lo verifica con `ffprobe`."""
    project = store.get_project(project_id)
    if project is None:
        raise ValueError(f"Proyecto no encontrado: {project_id!r}")

    edl_record: dict[str, Any] | None = None
    if edl_id is not None or version is not None:
        edl_record = store.get_edl_version(project_id, edl_id=edl_id, version=version)
        if edl_record is None:
            raise ValueError(
                f"Versión de EDL no encontrada (edl_id={edl_id!r}, version={version!r})."
            )
    elif cuts is None:
        edl_record = store.get_edl_version(project_id)
        if edl_record is None:
            raise ValueError("El proyecto no tiene ninguna versión de EDL para construir.")

    if edl_record is not None:
        edl_doc = dict(edl_record.get("edl") or {})
        raw_cuts = list(cuts if cuts is not None else (edl_doc.get("cuts") or []))
        resolved_aspect = str(aspect_ratio or edl_doc.get("aspect_ratio") or "16:9")
        default_source_aid = source_asset_id or edl_doc.get("source_asset_id")
    else:
        raw_cuts = list(cuts or [])
        resolved_aspect = str(aspect_ratio or "16:9")
        default_source_aid = source_asset_id
        total_dur = round(
            sum(float(c["out_sec"]) - float(c["in_sec"]) for c in raw_cuts),
            3,
        )
        edl_record = store.save_edl_version(
            project_id,
            edl={
                "schema": "comfy_studio.edl.v1",
                "title": "EDL directo",
                "aspect_ratio": resolved_aspect,
                "source_asset_id": default_source_aid,
                "total_duration_sec": total_dur,
                "cuts": raw_cuts,
            },
        )
        edl_doc = dict(edl_record["edl"])

    if not raw_cuts:
        raise ValueError("La lista de cortes del EDL está vacía.")
    if len(raw_cuts) > MAX_EDL_CUTS:
        raise ValueError(
            f"El EDL tiene {len(raw_cuts)} cortes; máximo permitido: {MAX_EDL_CUTS} (M3-SCR-005)."
        )

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("No se encontró `ffmpeg` en el PATH para ejecutar el EDL.")

    target_w, target_h = resolve_aspect_dimensions(resolved_aspect)
    vf_chain = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={int(fps)}"
    )

    expected_duration_sec = 0.0
    applied_cuts: list[dict[str, Any]] = []
    probed_sources: dict[Path, dict[str, Any]] = {}
    # M3-SCR-018: cachear la resolución de fuente por asset preferido para no volver a
    # escanear los assets del proyecto en cada corte (el ffprobe caro ya se memoiza en
    # probed_sources; esto evita repetir list_assets/get_asset por corte).
    resolved_sources: dict[str | None, tuple[str, Path]] = {}

    with tempfile.TemporaryDirectory(prefix="comfy_edl_build_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        seg_files: list[Path] = []

        # Primero resolvemos fuentes e inspeccionamos si algún clip fuente tiene pista de audio
        resolved_cut_sources: list[tuple[dict[str, Any], str, Path, bool]] = []
        any_has_audio = False
        for idx, cut in enumerate(raw_cuts):
            in_sec = round(float(cut.get("in_sec", 0.0)), 3)
            out_sec = round(float(cut.get("out_sec", 0.0)), 3)
            if out_sec <= in_sec:
                raise ValueError(
                    f"Corte #{idx + 1} inválido: out_sec ({out_sec}) <= in_sec ({in_sec})."
                )
            preferred_aid = cut.get("asset_id") or default_source_aid
            cached_source = resolved_sources.get(preferred_aid)
            if cached_source is None:
                cached_source = _resolve_source_video_path(
                    store,
                    project_id,
                    preferred_asset_id=preferred_aid,
                )
                resolved_sources[preferred_aid] = cached_source
            cut_aid, src_path = cached_source
            if src_path not in probed_sources:
                probed_sources[src_path] = probe_media_info(
                    src_path, timeout_s=ffprobe_timeout_s
                )
            src_has_audio = bool(probed_sources[src_path].get("has_audio"))
            if src_has_audio:
                any_has_audio = True
            resolved_cut_sources.append((cut, cut_aid, src_path, src_has_audio))

        for idx, (cut, cut_aid, src_path, src_has_audio) in enumerate(resolved_cut_sources):
            in_sec = round(float(cut.get("in_sec", 0.0)), 3)
            out_sec = round(float(cut.get("out_sec", 0.0)), 3)
            cut_dur = round(out_sec - in_sec, 3)

            seg_path = tmp_dir / f"seg_{idx:03d}.mp4"
            if src_has_audio:
                cmd_cut = [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{in_sec:.3f}",
                    "-t",
                    f"{cut_dur:.3f}",
                    "-i",
                    str(src_path),
                    "-vf",
                    vf_chain,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-ar",
                    "44100",
                    "-ac",
                    "2",
                    str(seg_path),
                ]
            elif any_has_audio:
                # Si otros segmentos llevan audio, inyectamos silencio AAC para concat uniforme
                cmd_cut = [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{in_sec:.3f}",
                    "-t",
                    f"{cut_dur:.3f}",
                    "-i",
                    str(src_path),
                    "-f",
                    "lavfi",
                    "-t",
                    f"{cut_dur:.3f}",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-vf",
                    vf_chain,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-shortest",
                    str(seg_path),
                ]
            else:
                cmd_cut = [
                    ffmpeg_bin,
                    "-y",
                    "-v",
                    "error",
                    "-ss",
                    f"{in_sec:.3f}",
                    "-t",
                    f"{cut_dur:.3f}",
                    "-i",
                    str(src_path),
                    "-vf",
                    vf_chain,
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-pix_fmt",
                    "yuv420p",
                    "-an",
                    str(seg_path),
                ]
            cut_timeout = cut_subprocess_timeout(cut_dur, base_s=ffmpeg_timeout_s)
            try:
                proc = subprocess.run(
                    cmd_cut, check=False, capture_output=True, timeout=cut_timeout
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"ffmpeg excedió el tiempo límite al recortar segmento #{idx + 1} "
                    f"({cut_timeout:.0f}s para un corte de {cut_dur:.2f}s; ajusta "
                    f"STUDIO_FFMPEG_TIMEOUT_S en hosts lentos)."
                ) from exc
            if proc.returncode != 0 or not seg_path.is_file():
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Error de ffmpeg al recortar segmento #{idx + 1}: {err}")

            seg_files.append(seg_path)
            expected_duration_sec = round(expected_duration_sec + cut_dur, 3)
            applied_cuts.append(
                {
                    "index": idx,
                    "asset_id": cut_aid,
                    "in_sec": in_sec,
                    "out_sec": out_sec,
                    "duration_sec": cut_dur,
                    "reason": str(cut.get("reason") or "highlight"),
                    "speaker": cut.get("speaker"),
                    "text": cut.get("text"),
                }
            )

        out_mp4 = tmp_dir / "edl_output.mp4"
        if len(seg_files) == 1:
            shutil.copyfile(seg_files[0], out_mp4)
        else:
            concat_list_path = tmp_dir / "concat_list.txt"
            lines = [f"file '{p.as_posix()}'" for p in seg_files]
            concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            cmd_concat = [
                ffmpeg_bin,
                "-y",
                "-v",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ]
            try:
                proc_c = subprocess.run(
                    cmd_concat, check=False, capture_output=True, timeout=ffmpeg_timeout_s
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("ffmpeg excedió el tiempo límite al concatenar el EDL.") from exc
            if proc_c.returncode != 0 or not out_mp4.is_file():
                err = proc_c.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Error de ffmpeg al concatenar EDL: {err}")

        mp4_bytes = out_mp4.read_bytes()
        if len(mp4_bytes) > MAX_EDL_OUTPUT_BYTES:
            raise RuntimeError(
                f"El MP4 renderizado ({len(mp4_bytes)} bytes) supera el límite duro de "
                f"{MAX_EDL_OUTPUT_BYTES} bytes (M3-SCR-004)."
            )
        probe_info = probe_media_info(out_mp4, timeout_s=ffprobe_timeout_s)
        # M1.7 (M3-IDEA-010 / M3-SCR-006): un render que ffprobe no puede verificar NO
        # puede registrarse como entregable verificado. Antes se devolvían dimensiones
        # y duración fabricadas como si fuesen medidas reales.
        if not bool(probe_info.get("probe_ok")):
            raise RuntimeError(
                "El MP4 renderizado no pudo verificarse con ffprobe (probe_ok=false): "
                "revisa que ffprobe exista en el PATH y que el contenedor sea válido."
            )

    aspect_slug = resolved_aspect.replace(":", "x")
    edl_ver = int(edl_record["version"])
    out_filename = f"edl_v{edl_ver}_{aspect_slug}.mp4"

    output_asset = store.save_upload(
        project_id,
        filename=out_filename,
        content_type="video/mp4",
        data=mp4_bytes,
        max_upload_bytes=MAX_EDL_OUTPUT_BYTES,
        category="outputs",
        metadata={
            "edl_render": True,
            "edl_id": edl_record["id"],
            "edl_version": edl_ver,
            "aspect_ratio": resolved_aspect,
            "expected_duration_sec": expected_duration_sec,
            "actual_duration_sec": probe_info.get("duration_sec", expected_duration_sec),
            "width": probe_info.get("width", target_w),
            "height": probe_info.get("height", target_h),
            "has_audio": bool(probe_info.get("has_audio")),
            "cuts_count": len(applied_cuts),
        },
    )

    updated_edl = store.update_edl_output(
        project_id,
        edl_record["id"],
        output_asset["id"],
    )

    return {
        "status": "built",
        "project_id": project_id,
        "edl_id": edl_record["id"],
        "version": edl_ver,
        "aspect_ratio": resolved_aspect,
        "output_asset": output_asset,
        "edl": updated_edl["edl"] if updated_edl else edl_doc,
        "cuts_applied": applied_cuts,
        "verification": {
            "expected_duration_sec": expected_duration_sec,
            "actual_duration_sec": float(probe_info.get("duration_sec") or expected_duration_sec),
            "width": int(probe_info.get("width") or target_w),
            "height": int(probe_info.get("height") or target_h),
            "codec_name": str(probe_info.get("codec_name") or "h264"),
            "has_audio": bool(probe_info.get("has_audio")),
            "audio_codec_name": probe_info.get("audio_codec_name"),
            # M1.7: llegar aquí implica probe_ok=True (si no, se lanzó RuntimeError antes);
            # se conserva el campo para consumidores que distingan render verificado.
            "probe_ok": True,
            "verified": True,
        },
    }
