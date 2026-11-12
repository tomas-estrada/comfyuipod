"""Reparacion localizada por intervalo temporal (C24 / A17), CPU-only."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ..media_bins import find_ffmpeg
from ..perception.keyframes import probe_media_info
from ..projects import sanitize_filename

DEFAULT_FFPROBE_TIMEOUT_S = 20.0
DEFAULT_FFMPEG_TIMEOUT_S = 60.0
MAX_LOCALIZED_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_FRAME_TOLERANCE = 8.0


class LocalizedRepairError(RuntimeError):
    """Error de validacion o ejecucion de la reparacion localizada."""


def _probe(path: Path, timeout_s: float) -> dict[str, Any]:
    return probe_media_info(path, timeout_s=timeout_s)


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, timeout=timeout)


def cut_subprocess_timeout(cut_duration_sec, *, base_s=DEFAULT_FFMPEG_TIMEOUT_S, cap_s=1800.0):
    base = max(1.0, float(base_s))
    cap = max(base, float(cap_s))
    scaled = 4.0 * max(0.0, float(cut_duration_sec)) + base
    return float(min(cap, max(base, scaled)))


try:
    from .ffmpeg_edl import cut_subprocess_timeout as _real_cut_timeout

    cut_subprocess_timeout = _real_cut_timeout
except Exception:
    pass


def _frame_rgb_mean(path, timestamp_sec, *, ffmpeg_bin, timeout_s=10.0):
    if not Path(path).is_file():
        return None
    duration = None
    try:
        info = probe_media_info(path, timeout_s=timeout_s)
        duration = float(info.get("duration_sec") or 0.0)
    except Exception:
        duration = None
    ts = max(0.0, float(timestamp_sec))
    if duration and duration > 0:
        ts = min(ts, max(0.0, duration - 0.05))
    cmd = [
        ffmpeg_bin,
        "-v",
        "error",
        "-ss",
        f"{ts:.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    try:
        proc = _run(cmd, timeout_s)
    except subprocess.TimeoutExpired:
        return None
    raw = proc.stdout or b""
    if proc.returncode != 0 or len(raw) < 3:
        return None
    n_pixels = len(raw) // 3
    if n_pixels <= 0:
        return None
    sr = sum(raw[i] for i in range(0, len(raw) - 2, 3))
    sg = sum(raw[i + 1] for i in range(0, len(raw) - 2, 3))
    sb = sum(raw[i + 2] for i in range(0, len(raw) - 2, 3))
    return [sr / n_pixels, sg / n_pixels, sb / n_pixels]


def _normalize_segment(
    ffmpeg_bin, src, out, *, start, duration, target_w, target_h, fps, timeout_s
):
    if duration <= 0:
        raise LocalizedRepairError("La duracion del segmento debe ser > 0.")
    vf = (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:black,"
        f"setsar=1,fps={int(fps)}"
    )
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, start):.3f}",
        "-t",
        f"{duration:.3f}",
        "-i",
        str(src),
        "-vf",
        vf,
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(out),
    ]
    try:
        proc = _run(cmd, cut_subprocess_timeout(duration, base_s=timeout_s))
    except subprocess.TimeoutExpired as exc:
        raise LocalizedRepairError("ffmpeg excedio el timeout normalizando un segmento.") from exc
    if proc.returncode != 0 or not Path(out).is_file():
        err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise LocalizedRepairError(f"Error de ffmpeg normalizando segmento: {err}")


def _frame_hashes(path, *, ffmpeg_bin, timeout_s):
    """Hash every decoded RGB frame without retaining raw frames in memory."""
    with tempfile.TemporaryFile(mode="w+b") as rows_file:
        cmd = [
            ffmpeg_bin,
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-f",
            "framehash",
            "-hash",
            "sha256",
            "pipe:1",
        ]
        try:
            proc = subprocess.run(
                cmd, check=False, stdout=rows_file, stderr=subprocess.PIPE, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalizedRepairError(
                "ffmpeg excedio el timeout calculando hashes de fotogramas."
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise LocalizedRepairError(f"Error de ffmpeg calculando hashes: {err}")
        rows_file.seek(0)
        hashes = []
        for raw_line in rows_file:
            line = raw_line.decode("ascii", errors="ignore").strip()
            if not line or line.startswith("#"):
                continue
            fields = [part.strip() for part in line.split(",")]
            if len(fields) < 6 or fields[0] != "0":
                raise LocalizedRepairError("ffmpeg devolvio una fila framehash invalida.")
            digest = fields[-1].lower()
            if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
                raise LocalizedRepairError("ffmpeg devolvio un SHA-256 de fotograma invalido.")
            hashes.append(bytes.fromhex(digest))
        if not hashes:
            raise LocalizedRepairError("No se pudieron leer hashes de fotogramas.")
        return hashes


def _ordered_hash_digest(hashes):
    digest = hashlib.sha256()
    for frame_hash in hashes:
        digest.update(frame_hash)
    return digest.hexdigest()


def _compare_unchanged_frames(
    head_hashes, replacement_frame_count, tail_hashes, assembled_hashes
):
    head_count = len(head_hashes)
    tail_count = len(tail_hashes)
    expected_count = head_count + replacement_frame_count + tail_count
    if len(assembled_hashes) != expected_count:
        return {
            "provenance_ok": False,
            "head_frame_count": head_count,
            "replacement_frame_count": replacement_frame_count,
            "tail_frame_count": tail_count,
            "assembled_frame_count": len(assembled_hashes),
            "expected_assembled_frame_count": expected_count,
            "head_hashes_match": False,
            "tail_hashes_match": False,
        }
    actual_head = assembled_hashes[:head_count]
    tail_start = head_count + replacement_frame_count
    actual_tail = assembled_hashes[tail_start:]
    head_ok = actual_head == head_hashes
    tail_ok = actual_tail == tail_hashes
    return {
        "provenance_ok": head_ok and tail_ok,
        "head_frame_count": head_count,
        "replacement_frame_count": replacement_frame_count,
        "tail_frame_count": tail_count,
        "assembled_frame_count": len(assembled_hashes),
        "expected_assembled_frame_count": expected_count,
        "head_hashes_match": head_ok,
        "tail_hashes_match": tail_ok,
        "head_expected_sha256": _ordered_hash_digest(head_hashes),
        "head_assembled_sha256": _ordered_hash_digest(actual_head),
        "tail_expected_sha256": _ordered_hash_digest(tail_hashes),
        "tail_assembled_sha256": _ordered_hash_digest(actual_tail),
        "comparison": "sha256_per_frame_pre_final_encode",
        "byte_identity_claimed": False,
        "lossy_final_encode": True,
    }


def _resolve_replacement_path(
    store, project_id, *, replacement_asset_id=None, replacement_path=None
):
    if replacement_path is not None:
        p = Path(replacement_path)
        if p.is_file():
            return p
        return None
    if replacement_asset_id:
        try:
            p = store.get_asset_path(project_id, replacement_asset_id)
        except Exception:
            p = None
        if p is not None and Path(p).is_file():
            return Path(p)
    return None


def build_localized_repair_video(
    source_path,
    replacement_path,
    output_path,
    *,
    edit_start_sec,
    edit_end_sec,
    ffmpeg_timeout_s=DEFAULT_FFMPEG_TIMEOUT_S,
    ffprobe_timeout_s=DEFAULT_FFPROBE_TIMEOUT_S,
    fps=24,
):
    src = Path(source_path)
    repl = Path(replacement_path)
    out = Path(output_path)
    if not src.is_file():
        raise LocalizedRepairError(f"Video fuente no encontrado: {src}")
    if not repl.is_file():
        raise LocalizedRepairError(f"Segmento de reemplazo no encontrado: {repl}")

    info = _probe(src, ffprobe_timeout_s)
    if not info.get("probe_ok"):
        raise LocalizedRepairError("El video fuente no pudo verificarse con ffprobe.")
    duration = float(info.get("duration_sec") or 0.0)
    if duration <= 0:
        raise LocalizedRepairError("El video fuente tiene duracion 0.")
    w = int(info.get("width") or 320)
    h = int(info.get("height") or 180)
    if not 1 <= int(fps) <= 120:
        raise LocalizedRepairError("fps debe estar entre 1 y 120.")
    if w < 1 or h < 1 or w * h > 33_177_600:
        raise LocalizedRepairError("La resolucion fuente supera el limite seguro de 8K.")

    if float(edit_end_sec) > duration + 1e-3:
        raise LocalizedRepairError(
            f"edit_end_sec ({edit_end_sec}) supera la duracion del fuente ({duration})."
        )
    start = round(max(0.0, float(edit_start_sec)), 3)
    end = round(min(duration, float(edit_end_sec)), 3)
    if end <= start:
        raise LocalizedRepairError(f"Intervalo de edicion invalido: {start}..{end}")

    repl_info = _probe(repl, ffprobe_timeout_s)
    repl_duration = float(repl_info.get("duration_sec") or 0.0)
    if repl_duration <= 0:
        raise LocalizedRepairError("El segmento de reemplazo tiene duracion 0.")
    if abs(repl_duration - (end - start)) > max(1.0 / float(fps), 0.02):
        raise LocalizedRepairError(
            "La duracion del segmento de reemplazo debe coincidir con el intervalo "
            f"(intervalo={end - start:.3f}s, segmento={repl_duration:.3f}s)."
        )

    head_dur = start
    tail_start = end
    tail_dur = round(duration - end, 3)

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise LocalizedRepairError("No se encontro ffmpeg en el PATH.")

    with tempfile.TemporaryDirectory(prefix="comfy_localized_repair_") as tmp_str:
        tmp = Path(tmp_str)
        segs = []
        if head_dur > 1e-3:
            head_path = tmp / "head.mkv"
            _normalize_segment(
                ffmpeg_bin,
                src,
                head_path,
                start=0.0,
                duration=head_dur,
                target_w=w,
                target_h=h,
                fps=fps,
                timeout_s=ffmpeg_timeout_s,
            )
            segs.append(head_path)
        repl_path = tmp / "repl.mkv"
        _normalize_segment(
            ffmpeg_bin,
            repl,
            repl_path,
            start=0.0,
            duration=repl_duration,
            target_w=w,
            target_h=h,
            fps=fps,
            timeout_s=ffmpeg_timeout_s,
        )
        segs.append(repl_path)
        if tail_dur > 1e-3:
            tail_path = tmp / "tail.mkv"
            _normalize_segment(
                ffmpeg_bin,
                src,
                tail_path,
                start=tail_start,
                duration=tail_dur,
                target_w=w,
                target_h=h,
                fps=fps,
                timeout_s=ffmpeg_timeout_s,
            )
            segs.append(tail_path)

        assembled = tmp / "assembled.mkv"
        if len(segs) == 1:
            import shutil as _shutil

            _shutil.copyfile(segs[0], assembled)
        else:
            concat_list = tmp / "concat.txt"
            concat_list.write_text(
                "\n".join(f"file '{p.as_posix()}'" for p in segs) + "\n", encoding="utf-8"
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
                str(concat_list),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(assembled),
            ]
            try:
                proc = _run(cmd, ffmpeg_timeout_s)
            except subprocess.TimeoutExpired as exc:
                raise LocalizedRepairError("ffmpeg excedio el timeout concatenando.") from exc
            if proc.returncode != 0 or not assembled.is_file():
                err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
                raise LocalizedRepairError(f"Error de ffmpeg concatenando: {err}")

        head_hashes = (
            _frame_hashes(
                head_path,
                ffmpeg_bin=ffmpeg_bin,
                timeout_s=cut_subprocess_timeout(head_dur, base_s=ffmpeg_timeout_s),
            )
            if head_dur > 1e-3
            else []
        )
        replacement_hashes = _frame_hashes(
            repl_path,
            ffmpeg_bin=ffmpeg_bin,
            timeout_s=cut_subprocess_timeout(repl_duration, base_s=ffmpeg_timeout_s),
        )
        tail_hashes = (
            _frame_hashes(
                tail_path,
                ffmpeg_bin=ffmpeg_bin,
                timeout_s=cut_subprocess_timeout(tail_dur, base_s=ffmpeg_timeout_s),
            )
            if tail_dur > 1e-3
            else []
        )
        assembled_hashes = _frame_hashes(
            assembled,
            ffmpeg_bin=ffmpeg_bin,
            timeout_s=cut_subprocess_timeout(duration, base_s=ffmpeg_timeout_s),
        )
        frame_provenance = _compare_unchanged_frames(
            head_hashes, len(replacement_hashes), tail_hashes, assembled_hashes
        )
        if not frame_provenance["provenance_ok"]:
            raise LocalizedRepairError(
                "Los hashes SHA-256 de los fotogramas sin editar no coinciden en la asamblea "
                "sin pérdida previa al encode final."
            )

        # Mantener las pistas de audio originales en toda la duración del video.
        # El mapa opcional permite fuentes que no tienen audio.
        remux = [
            ffmpeg_bin,
            "-y",
            "-v",
            "error",
            "-i",
            str(assembled),
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-t",
            f"{duration:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
        try:
            proc = _run(remux, ffmpeg_timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise LocalizedRepairError("ffmpeg excedió el timeout remuxeando el audio.") from exc
        if proc.returncode != 0 or not out.is_file():
            err = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            raise LocalizedRepairError(f"Error de ffmpeg remuxeando audio: {err}")

        probe = _probe(out, ffprobe_timeout_s)
        if not probe.get("probe_ok"):
            raise LocalizedRepairError("El MP4 reparado no pudo verificarse con ffprobe.")

    return {
        "output_path": str(out),
        "duration_sec": float(probe.get("duration_sec") or 0.0),
        "width": int(probe.get("width") or w),
        "height": int(probe.get("height") or h),
        "edit_start_sec": start,
        "edit_end_sec": end,
        "source_duration_sec": duration,
        "replacement_duration_sec": repl_duration,
        "frame_provenance": frame_provenance,
        "verification": {
            "probe_ok": True,
            "width": int(probe.get("width") or w),
            "height": int(probe.get("height") or h),
            "duration_sec": float(probe.get("duration_sec") or 0.0),
            "codec_name": str(probe.get("codec_name") or "h264"),
            "has_audio": bool(probe.get("has_audio")),
        },
    }


def verify_source_frame_provenance(
    source_path,
    output_path,
    *,
    edit_start_sec,
    edit_end_sec,
    sample_count=6,
    tolerance=DEFAULT_FRAME_TOLERANCE,
    ffmpeg_timeout_s=DEFAULT_FFMPEG_TIMEOUT_S,
    ffprobe_timeout_s=DEFAULT_FFPROBE_TIMEOUT_S,
):
    src = Path(source_path)
    out = Path(output_path)
    if not src.is_file():
        raise LocalizedRepairError(f"Video fuente no encontrado: {src}")
    if not out.is_file():
        raise LocalizedRepairError(f"Salida no encontrada: {out}")

    src_info = _probe(src, ffprobe_timeout_s)
    out_info = _probe(out, ffprobe_timeout_s)
    if not src_info.get("probe_ok") or not out_info.get("probe_ok"):
        raise LocalizedRepairError("No se pudo verificar fuente o salida con ffprobe.")

    src_duration = float(src_info.get("duration_sec") or 0.0)
    out_duration = float(out_info.get("duration_sec") or 0.0)
    start = max(0.0, float(edit_start_sec))
    end = min(src_duration, float(edit_end_sec)) if src_duration > 0 else float(edit_end_sec)

    ffmpeg_bin = find_ffmpeg()
    if not ffmpeg_bin:
        raise LocalizedRepairError("No se encontro ffmpeg en el PATH.")

    timestamps = []
    if start > 0.05:
        n_before = max(1, sample_count // 2)
        for i in range(n_before):
            t = (i + 1) * start / (n_before + 1)
            if 0 <= t < start - 1e-3:
                timestamps.append(round(t, 3))
    if src_duration > end + 0.05:
        n_after = max(1, sample_count - len(timestamps))
        remaining = src_duration - end
        for i in range(n_after):
            t = end + (i + 1) * remaining / (n_after + 1)
            if t <= src_duration - 1e-3:
                timestamps.append(round(t, 3))

    seen = set()
    uniq = []
    for t in timestamps:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    timestamps = uniq[:sample_count]

    samples = []
    max_delta = 0.0
    all_ok = True
    for t in timestamps:
        src_mean = _frame_rgb_mean(src, t, ffmpeg_bin=ffmpeg_bin, timeout_s=ffprobe_timeout_s)
        out_mean = _frame_rgb_mean(out, t, ffmpeg_bin=ffmpeg_bin, timeout_s=ffprobe_timeout_s)
        if src_mean is None or out_mean is None:
            samples.append(
                {
                    "timestamp_sec": t,
                    "source_rgb_mean": src_mean,
                    "output_rgb_mean": out_mean,
                    "delta": None,
                    "within_tolerance": False,
                    "reason": "no se pudo medir el fotograma",
                }
            )
            all_ok = False
            continue
        delta = max(abs(src_mean[i] - out_mean[i]) for i in range(3))
        if delta > max_delta:
            max_delta = delta
        within = delta <= tolerance
        if not within:
            all_ok = False
        samples.append(
            {
                "timestamp_sec": t,
                "source_rgb_mean": [round(x, 2) for x in src_mean],
                "output_rgb_mean": [round(x, 2) for x in out_mean],
                "delta": round(delta, 2),
                "within_tolerance": within,
            }
        )

    if not timestamps:
        all_ok = False

    return {
        "provenance_ok": bool(all_ok and len(timestamps) > 0),
        "sample_count": len(timestamps),
        "samples": samples,
        "max_delta_rgb": round(max_delta, 2),
        "tolerance_rgb": float(tolerance),
        "edit_start_sec": round(start, 3),
        "edit_end_sec": round(end, 3),
        "source_duration_sec": round(src_duration, 3),
        "output_duration_sec": round(out_duration, 3),
        "byte_identity_claimed": False,
        "lossy_encode": True,
        "comparison": "frame_rgb_mean_tolerance",
    }


def localized_repair(
    store,
    project_id,
    *,
    source_asset_id,
    replacement_asset_id=None,
    replacement_path=None,
    edit_start_sec,
    edit_end_sec,
    output_filename=None,
    ffmpeg_timeout_s=DEFAULT_FFMPEG_TIMEOUT_S,
    ffprobe_timeout_s=DEFAULT_FFPROBE_TIMEOUT_S,
    frame_tolerance=DEFAULT_FRAME_TOLERANCE,
    fps=24,
):
    project = store.get_project(project_id)
    if project is None:
        raise LocalizedRepairError(f"Proyecto no encontrado: {project_id!r}")

    src_path = store.get_asset_path(project_id, source_asset_id)
    if src_path is None:
        raise LocalizedRepairError(f"Asset fuente no encontrado: {source_asset_id!r}")

    repl_path = _resolve_replacement_path(
        store,
        project_id,
        replacement_asset_id=replacement_asset_id,
        replacement_path=replacement_path,
    )
    if repl_path is None:
        raise LocalizedRepairError("No se pudo resolver el segmento de reemplazo (asset o ruta).")

    info = _probe(src_path, ffprobe_timeout_s)
    if not info.get("probe_ok"):
        raise LocalizedRepairError("El video fuente no pudo verificarse con ffprobe.")

    safe_filename = (
        sanitize_filename(output_filename, fallback_ext=".mp4") if output_filename else None
    )
    if safe_filename and Path(safe_filename).suffix.lower() != ".mp4":
        raise LocalizedRepairError("El nombre de salida debe terminar en .mp4.")
    with tempfile.TemporaryDirectory(prefix="comfy_localized_repair_store_") as tmp_str:
        tmp = Path(tmp_str)
        out_file = tmp / (safe_filename or "localized_repair.mp4")
        build = build_localized_repair_video(
            src_path,
            repl_path,
            out_file,
            edit_start_sec=edit_start_sec,
            edit_end_sec=edit_end_sec,
            ffmpeg_timeout_s=ffmpeg_timeout_s,
            ffprobe_timeout_s=ffprobe_timeout_s,
            fps=fps,
        )
        provenance = build["frame_provenance"]
        post_encode_diagnostic = verify_source_frame_provenance(
            src_path,
            out_file,
            edit_start_sec=build["edit_start_sec"],
            edit_end_sec=build["edit_end_sec"],
            tolerance=frame_tolerance,
            ffmpeg_timeout_s=ffmpeg_timeout_s,
            ffprobe_timeout_s=ffprobe_timeout_s,
        )
        if not provenance["provenance_ok"]:
            raise LocalizedRepairError(
                "Los hashes SHA-256 de fotogramas sin editar no coinciden antes del encode "
                "final; la salida no se registró en el proyecto."
            )
        provenance["post_encode_rgb_diagnostic"] = post_encode_diagnostic

        if out_file.stat().st_size > MAX_LOCALIZED_OUTPUT_BYTES:
            raise LocalizedRepairError("El MP4 reparado supera el limite de tamano.")
        mp4_bytes = out_file.read_bytes()

        out_name = safe_filename or f"localized_repair_{source_asset_id[:8]}.mp4"
        output_asset = store.save_upload(
            project_id,
            filename=out_name,
            content_type="video/mp4",
            data=mp4_bytes,
            max_upload_bytes=MAX_LOCALIZED_OUTPUT_BYTES,
            category="outputs",
            metadata={
                "localized_repair": True,
                "source_asset_id": source_asset_id,
                "replacement_asset_id": replacement_asset_id,
                "edit_start_sec": build["edit_start_sec"],
                "edit_end_sec": build["edit_end_sec"],
                "source_duration_sec": build["source_duration_sec"],
                "replacement_duration_sec": build["replacement_duration_sec"],
                "output_duration_sec": build["duration_sec"],
                "width": build["width"],
                "height": build["height"],
                "frame_provenance_ok": provenance["provenance_ok"],
                "frame_provenance_head_frame_count": provenance["head_frame_count"],
                "frame_provenance_tail_frame_count": provenance["tail_frame_count"],
                "frame_provenance_head_expected_sha256": provenance.get("head_expected_sha256"),
                "frame_provenance_head_assembled_sha256": provenance.get("head_assembled_sha256"),
                "frame_provenance_tail_expected_sha256": provenance.get("tail_expected_sha256"),
                "frame_provenance_tail_assembled_sha256": provenance.get("tail_assembled_sha256"),
                "frame_post_encode_rgb_ok": post_encode_diagnostic["provenance_ok"],
                "frame_post_encode_rgb_max_delta": post_encode_diagnostic["max_delta_rgb"],
                "frame_post_encode_rgb_tolerance": post_encode_diagnostic["tolerance_rgb"],
                "frame_check_method": provenance["comparison"],
                "frame_check_sample_count": post_encode_diagnostic["sample_count"],
                "byte_identity_claimed": False,
            },
        )

    return {
        "status": "repaired",
        "project_id": project_id,
        "source_asset_id": source_asset_id,
        "replacement_asset_id": replacement_asset_id,
        "output_asset": output_asset,
        "edit_start_sec": build["edit_start_sec"],
        "edit_end_sec": build["edit_end_sec"],
        "build": build,
        "provenance": provenance,
        "verification": {
            **build["verification"],
            "frame_provenance_ok": provenance["provenance_ok"],
            "frame_post_encode_rgb_ok": post_encode_diagnostic["provenance_ok"],
            "byte_identity_claimed": False,
        },
    }


localized_repair_video = localized_repair
repair_localized = localized_repair
perform_localized_repair = localized_repair
verify_frame_provenance = verify_source_frame_provenance

__all__ = [
    "LocalizedRepairError",
    "build_localized_repair_video",
    "localized_repair",
    "localized_repair_video",
    "repair_localized",
    "perform_localized_repair",
    "verify_source_frame_provenance",
    "verify_frame_provenance",
    "DEFAULT_FRAME_TOLERANCE",
]
