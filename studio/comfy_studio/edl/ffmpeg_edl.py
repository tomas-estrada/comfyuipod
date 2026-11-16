"""Ejecutor determinista de EDL sobre `ffmpeg` (corte, reencuadre 16:9 / 9:16 y concatenación).

Cumple `VAL-PERC-006` y `VAL-PROD-001`:
- Recorta cada segmento `[in_sec, out_sec]` en el orden exacto de la lista de cortes.
- Adapta la relación de aspecto de entrega (`16:9` horizontal, `9:16` vertical para reels/shorts,
  `1:1` o `4:5`) con escalado y letterbox/pillarbox determinista.
- Concatena los segmentos en un contenedor MP4 final, lo registra en la galería del proyecto
  (`outputs/`) y verifica duración y dimensiones reales con `ffprobe`.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..media_bins import find_ffmpeg
from ..perception.keyframes import probe_media_info
from ..projects import ProjectStore

ASPECT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
    "1:1": (720, 720),
    "4:5": (720, 900),
}
FOUR_K_DIMENSIONS: dict[str, tuple[int, int]] = {
    "16:9": (3840, 2160),
    "9:16": (2160, 3840),
    "1:1": (2160, 2160),
}

# M3-SCR-005: cota defensiva de cortes por EDL (cada corte = 1 subprocess ffmpeg)
MAX_EDL_CUTS = 200
# M3-SCR-004: cota DURA del MP4 de salida. No se calcula a partir del propio tamaño
# (max(N, len+N) nunca rechaza nada): 720p H.264 de un EDL acotado queda muy por debajo.
MAX_EDL_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB

# A22 (política de color de entrega): `color_space="bt709"` en la petición EDL
# declara una entrega BT.709 real. La conversión se hace por fuente con su
# matriz declarada (`colorspace=iall=<matriz>:all=bt709:fast=1`: solo cambio de
# matriz, sin shift de gamma) y la salida se etiqueta en VUI (setparams + flags
# del encoder). La verificación exige bt709 en color_space/color_transfer/
# color_primaries del probe real. Sin matriz conocida en la fuente NO se
# convierte ni se reetiqueta: fallo cerrado con un error útil.
EDL_COLOR_SPACES = {"bt709"}
BT709_SOURCE_MATRICES = {"bt709", "smpte170m", "bt470bg"}
HDR_COLOR_TRANSFERS = {"smpte2084", "arib-std-b67"}
HDR_COLOR_PRIMARIES = {"bt2020", "smpte428", "smpte431"}

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


def resolve_aspect_dimensions(aspect_ratio: str, resolution: str = "standard") -> tuple[int, int]:
    """Return the exact output canvas for an aspect ratio and resolution profile."""
    cleaned = (aspect_ratio or "16:9").strip()
    if resolution == "4k":
        if cleaned not in FOUR_K_DIMENSIONS:
            raise ValueError(
                f"No hay dimensiones 4K aprobadas para {cleaned!r}; usa 16:9, 9:16 o 1:1."
            )
        return FOUR_K_DIMENSIONS[cleaned]
    if resolution != "standard":
        raise ValueError("La resolución debe ser 'standard' o '4k'.")
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


def _resolve_bt709_source_matrix(asset_id: str, source_info: dict[str, Any]) -> str | None:
    """Matriz de color declarada de la fuente para la conversión BT.709.

    Devuelve `None` si la fuente ya declara bt709 (el filtro `colorspace` no hace
    falta). Falla cerrado si la fuente no declara una matriz soportada o si
    declara transferencia/primarios HDR: en esos casos no se puede entregar
    SDR BT.709 de forma honesta y no se reetiqueta la salida.
    """
    # A22: los checks HDR van ANTES del early return de matriz bt709; si la
    # fuente declarara matriz BT.709 con transferencia/primarios HDR, un
    # return anticipado los silenciaría y el tag VUI engañoso pasaría como
    # entrega SDR verificada. Fail-closed primero, matriz después.
    transfer = source_info.get("color_transfer")
    if transfer in HDR_COLOR_TRANSFERS:
        raise RuntimeError(
            "Política de color BT.709: la fuente "
            f"{asset_id!r} usa transferencia HDR ({transfer!r}); esta entrega no "
            "soporta tonemapping HDR→SDR. Usa color_space=None o una fuente SDR."
        )
    primaries = source_info.get("color_primaries")
    if primaries in HDR_COLOR_PRIMARIES:
        raise RuntimeError(
            "Política de color BT.709: la fuente "
            f"{asset_id!r} usa primarios HDR ({primaries!r}); esta entrega no "
            "soporta conversión de gamuta. Usa color_space=None o una fuente SDR."
        )
    matrix = source_info.get("color_space")
    if matrix == "bt709":
        return None
    if matrix not in BT709_SOURCE_MATRICES:
        raise RuntimeError(
            "Política de color BT.709: la fuente "
            f"{asset_id!r} no declara una matriz de color convertible "
            f"(color_space={matrix!r}; soportadas: {sorted(BT709_SOURCE_MATRICES)}). "
            "Re-etiqueta la fuente con su matriz real (p. ej. `ffmpeg -colorspace "
            "smpte170m`) antes de renderizar con color_space=bt709; una fuente con "
            "metadatos de color desconocidos no se convierte ni se reetiqueta."
        )
    return matrix


def execute_edl_ffmpeg(
    store: ProjectStore,
    project_id: str,
    *,
    edl_id: str | None = None,
    version: int | None = None,
    aspect_ratio: str | None = None,
    resolution: str = "standard",
    source_asset_id: str | None = None,
    cuts: list[dict[str, Any]] | None = None,
    fps: int = 24,
    color_range: str = "limited",
    color_space: str | None = None,
    picture_inset_percent: float = 0.0,
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

    # A22: la política de color llega por parámetro o, si no, del EDL guardado
    # (`EdlCreateRequest` la persiste como `color_space`). Se valida antes de
    # arrancar cualquier subprocess.
    requested_color_space: str | None = (
        color_space
        if color_space is not None
        else (str(edl_doc.get("color_space")) if edl_doc.get("color_space") else None)
    )
    if requested_color_space is not None and requested_color_space not in EDL_COLOR_SPACES:
        raise ValueError(
            f"color_space no soportado: {requested_color_space!r}. Esta entrega "
            "solo garantiza la conversión y etiquetado a 'bt709' (con matrices de "
            "fuente conocidas); cualquier otra política falla cerrado."
        )

    if not raw_cuts:
        raise ValueError("La lista de cortes del EDL está vacía.")
    if len(raw_cuts) > MAX_EDL_CUTS:
        raise ValueError(
            f"El EDL tiene {len(raw_cuts)} cortes; máximo permitido: {MAX_EDL_CUTS} (M3-SCR-005)."
        )
    if isinstance(fps, bool) or not isinstance(fps, int) or not 12 <= fps <= 60:
        raise ValueError("fps debe estar entre 12 y 60.")
    if color_range not in {"limited", "full"}:
        raise ValueError("color_range debe ser 'limited' o 'full'.")
    if (
        not math.isfinite(float(picture_inset_percent))
        or not 0 <= float(picture_inset_percent) <= 25
    ):
        raise ValueError("picture_inset_percent debe estar entre 0 y 25.")

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise RuntimeError("No se encontró `ffmpeg` en el PATH para ejecutar el EDL.")

    target_w, target_h = resolve_aspect_dimensions(resolved_aspect, resolution)
    inset = float(picture_inset_percent) / 100.0
    if inset:
        inner_w = max(2, int(target_w * (1.0 - 2.0 * inset)) & ~1)
        inner_h = max(2, int(target_h * (1.0 - 2.0 * inset)) & ~1)
    else:
        inner_w, inner_h = target_w, target_h
    output_range = "tv" if color_range == "limited" else "pc"
    full_range_flag = "1" if color_range == "full" else "0"
    # A22: con la política BT.709 activa el encoder recibe los flags VUI de la
    # entrega. `setparams` ya etiqueta los frames (es lo que permite que x264
    # escriba matriz + primarios + transferencia en el VUI); los flags del
    # encoder los fijan también a nivel de codificador.
    vui_args = (
        [
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
        ]
        if requested_color_space == "bt709"
        else []
    )

    def _vf_chain(input_range: str | None, source_matrix: str | None) -> str:
        # ffprobe may not know the source range. Assume limited in that case,
        # matching the conventional interpretation of ordinary yuv420p video.
        source_range = input_range if input_range in {"tv", "pc"} else "tv"
        filters = [
            f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:"
            f"in_range={source_range}:out_range={source_range}",
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black",
            "setsar=1",
            f"fps={int(fps)}",
            f"scale={target_w}:{target_h}:in_range={source_range}:out_range={output_range}",
            "format=yuv420p",
        ]
        if requested_color_space == "bt709":
            if source_matrix is not None:
                # Conversión real de matriz (BT.601→BT.709) con la matriz
                # declarada de la fuente; `fast=1` mantiene la transferencia y
                # evita un shift de gamma no deseado. Sin matriz conocida la
                # política ya habría fallado cerrado antes de llegar aquí.
                filters.append(f"colorspace=iall={source_matrix}:all=bt709:fast=1")
            # setparams sobre el frame es lo que hace que el encoder escriba el
            # VUI completo (matriz, primarios y transferencia).
            filters.append(
                f"setparams=range={output_range}:color_primaries=bt709:"
                "color_trc=bt709:colorspace=bt709"
            )
        else:
            filters.append(f"setparams=range={output_range}")
        return ",".join(filters)

    expected_duration_sec = 0.0
    applied_cuts: list[dict[str, Any]] = []
    probed_sources: dict[Path, dict[str, Any]] = {}
    # A22: matriz de color declarada por fuente (cached por path); None si la
    # fuente ya es bt709 y no hace falta el filtro `colorspace`.
    source_matrices: dict[Path, str | None] = {}
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
                probed_sources[src_path] = probe_media_info(src_path, timeout_s=ffprobe_timeout_s)
                if not bool(probed_sources[src_path].get("probe_ok")):
                    raise RuntimeError(
                        f"El video fuente del corte #{idx + 1} no pudo verificarse con ffprobe."
                    )
                if requested_color_space == "bt709":
                    source_matrices[src_path] = _resolve_bt709_source_matrix(
                        cut_aid, probed_sources[src_path]
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
            vf_chain = _vf_chain(
                probed_sources[src_path].get("color_range"),
                source_matrices.get(src_path),
            )
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
                    "-color_range",
                    output_range,
                    *vui_args,
                    "-bsf:v",
                    f"h264_metadata=video_full_range_flag={full_range_flag}",
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
                    "-color_range",
                    output_range,
                    *vui_args,
                    "-bsf:v",
                    f"h264_metadata=video_full_range_flag={full_range_flag}",
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
                    "-color_range",
                    output_range,
                    *vui_args,
                    "-bsf:v",
                    f"h264_metadata=video_full_range_flag={full_range_flag}",
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
        actual_size = (probe_info.get("width"), probe_info.get("height"))
        if actual_size != (target_w, target_h):
            raise RuntimeError(
                "El MP4 renderizado no coincide con las dimensiones solicitadas: "
                f"esperadas {target_w}x{target_h}, obtenidas "
                f"{actual_size[0]}x{actual_size[1]}."
            )
        probed_fps = float(probe_info.get("fps") or 0.0)
        if abs(probed_fps - fps) > 0.5:
            raise RuntimeError(
                "El MP4 renderizado no coincide con los FPS solicitados: "
                f"esperados {fps}, obtenidos {probed_fps:g}."
            )
        expected_range = "pc" if color_range == "full" else "tv"
        probed_range = probe_info.get("color_range")
        if probed_range != expected_range:
            raise RuntimeError(
                "El MP4 renderizado no coincide con el rango de color solicitado: "
                f"esperado {expected_range}, obtenido {probed_range!r}."
            )
        if requested_color_space == "bt709":
            # A22: la entrega BT.709 se verifica contra el probe real; no basta
            # con haber escrito los tags (podría ser un reetiquetado sin
            # conversión). La conversión real la garantiza el fail-closed por
            # fuente + `colorspace` con la matriz declarada.
            probed_tags = (
                probe_info.get("color_space"),
                probe_info.get("color_transfer"),
                probe_info.get("color_primaries"),
            )
            if probed_tags != ("bt709", "bt709", "bt709"):
                raise RuntimeError(
                    "El MP4 renderizado no verifica la política de color BT.709: "
                    "probe real (color_space, color_transfer, color_primaries) = "
                    f"{probed_tags!r}, esperado ('bt709', 'bt709', 'bt709')."
                )

    # A22: evidencia de color de las fuentes usadas (dedup por path, primer
    # asset_id que la usó) y valores solicitados vs sondeados de la salida.
    source_color_metadata: list[dict[str, Any]] = []
    if requested_color_space == "bt709":
        seen_source_paths: set[Path] = set()
        for cut_aid, src_path in resolved_sources.values():
            if src_path in seen_source_paths:
                continue
            seen_source_paths.add(src_path)
            source_info = probed_sources[src_path]
            source_color_metadata.append(
                {
                    "asset_id": cut_aid,
                    "color_space": source_info.get("color_space"),
                    "color_transfer": source_info.get("color_transfer"),
                    "color_primaries": source_info.get("color_primaries"),
                }
            )

    aspect_slug = resolved_aspect.replace(":", "x")
    edl_ver = int(edl_record["version"])
    resolution_slug = "4k" if resolution == "4k" else "720p"
    out_filename = f"edl_v{edl_ver}_{aspect_slug}_{resolution_slug}.mp4"

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
            "resolution": resolution,
            "expected_duration_sec": expected_duration_sec,
            "actual_duration_sec": probe_info.get("duration_sec", expected_duration_sec),
            "width": probe_info.get("width", target_w),
            "height": probe_info.get("height", target_h),
            "has_audio": bool(probe_info.get("has_audio")),
            "cuts_count": len(applied_cuts),
            "fps": int(fps),
            "color_range": color_range,
            "picture_inset_percent": float(picture_inset_percent),
            **(
                {"color_space": requested_color_space}
                if requested_color_space is not None
                else {}
            ),
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
        "resolution": resolution,
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
            "fps": int(fps),
            "probed_fps": probe_info.get("fps"),
            "color_range": color_range,
            "probed_color_range": probe_info.get("color_range"),
            "picture_inset_percent": float(picture_inset_percent),
            # A22: con la política de color activa se exponen el valor
            # solicitado, los tags realmente sondeados en la salida y la
            # evidencia de color de cada fuente (raw ffprobe; None = sin tag).
            **(
                {
                    "color_space": requested_color_space,
                    "probed_color_space": probe_info.get("color_space"),
                    "probed_color_transfer": probe_info.get("color_transfer"),
                    "probed_color_primaries": probe_info.get("color_primaries"),
                    "source_color_metadata": source_color_metadata,
                }
                if requested_color_space is not None
                else {}
            ),
            # M1.7: llegar aquí implica probe_ok=True (si no, se lanzó RuntimeError antes);
            # se conserva el campo para consumidores que distingan render verificado.
            "probe_ok": True,
            "verified": True,
        },
    }
