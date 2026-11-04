"""Pipeline del estudio de anuncios: persistencia de anuncios y variantes de formato.

`run_ad_variants` monta, por cada aspect ratio pedido, un MP4 a partir de los
planos del storyboard (assets de video o imagen del proyecto) con `ffmpeg`,
mezcla opcionalmente voz (TTS del motor `audio`) y música (motor `music_gen`),
evalúa cada variante con el motor `qa` y registra todo como assets del proyecto.

Modo stub (`STUDIO_STUB_AD_STUDIO=1`, vía `simulated=True`): escribe artefactos
mínimos y marca `simulated: true` en vez de fingir render real.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from comfy_studio.ads.schema import (
    AdBrief,
    AdScript,
    AdStoryboard,
    ad_doc_dict,
    build_storyboard,
)
from comfy_studio.config import SecretRedactor, StudioConfig
from comfy_studio.edl.ffmpeg_edl import (
    ASPECT_DIMENSIONS,
    DEFAULT_FFMPEG_TIMEOUT_S,
    DEFAULT_FFPROBE_TIMEOUT_S,
    MAX_EDL_CUTS,
)
from comfy_studio.engines import EngineRunSpec
from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.perception.keyframes import probe_media_info
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

ADS_INDEX_KEY = "ads:index"

_STUB_WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16
_STUB_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 16

DEFAULT_AD_ASPECTS: tuple[str, ...] = ("9:16", "1:1", "16:9")
DEFAULT_AD_ACCEPTANCE: dict[str, Any] = {"max_black_s": 0.5, "max_freeze_s": 1.0}
MAX_AD_VARIANTS = 4


class AdPipelineError(RuntimeError):
    """Fallo tipado del pipeline de anuncios (validación/ejecución)."""


class AdCancelledError(AdPipelineError):
    """El job asociado fue cancelado: se aborta entre variantes (sticky)."""


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ad_key(ad_id: str) -> str:
    return f"ad:{ad_id}"


def create_ad_doc(store: ProjectStore, project_id: str, brief: AdBrief) -> dict[str, Any]:
    """Crea y persiste un anuncio nuevo con su guion compuesto (determinista)."""
    from .schema import compose_script

    if store.get_project(project_id) is None:
        raise AdPipelineError(f"Proyecto no encontrado: {project_id!r}")
    ad_id = uuid.uuid4().hex[:12]
    now = _utcnow()
    script: AdScript = compose_script(brief)
    doc = ad_doc_dict(
        ad_id=ad_id,
        project_id=project_id,
        brief=script.brief,
        script=script,
        created_at=now,
        updated_at=now,
    )
    _save(store, project_id, doc)
    return doc


def get_ad_doc(store: ProjectStore, project_id: str, ad_id: str) -> dict[str, Any] | None:
    raw = store.get_meta(project_id, ad_key(ad_id))
    if not isinstance(raw, dict):
        return None
    return raw


def list_ad_docs(store: ProjectStore, project_id: str) -> list[dict[str, Any]]:
    index = store.get_meta(project_id, ADS_INDEX_KEY) or []
    docs: list[dict[str, Any]] = []
    for ad_id in index:
        doc = get_ad_doc(store, project_id, str(ad_id))
        if doc is not None:
            docs.append(doc)
    return docs


def save_ad_doc(store: ProjectStore, project_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    return _save(store, project_id, doc)


def _save(store: ProjectStore, project_id: str, doc: dict[str, Any]) -> dict[str, Any]:
    doc["updated_at"] = _utcnow()
    store.set_meta(project_id, ad_key(str(doc["id"])), doc)
    index = store.get_meta(project_id, ADS_INDEX_KEY) or []
    if doc["id"] not in index:
        index.append(doc["id"])
        store.set_meta(project_id, ADS_INDEX_KEY, index)
    return doc


def attach_storyboard(store: ProjectStore, project_id: str, ad_id: str) -> dict[str, Any]:
    """Genera y persiste el storyboard del anuncio a partir de su guion."""
    doc = get_ad_doc(store, project_id, ad_id)
    if doc is None:
        raise AdPipelineError(f"Anuncio no encontrado: {ad_id!r}")
    raw_script = doc.get("script")
    if not raw_script:
        raise AdPipelineError(f"El anuncio {ad_id!r} no tiene guion todavía.")
    script = AdScript.model_validate(raw_script)
    storyboard = build_storyboard(script, ad_id=ad_id)
    doc["storyboard"] = storyboard.to_dict()
    doc.setdefault("gate", {})["state"] = "awaiting_approval"
    return _save(store, project_id, doc)


# ---------------------------------------------------------------------------
# Variantes de formato
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    path: Path
    kind: str  # "video" | "image"
    duration_s: float
    asset_id: str
    media_duration_s: float = 0.0


def _check_cancelled(store: ProjectStore, job_id: str | None) -> None:
    if not job_id:
        return
    job = store.get_job(job_id)
    if job is not None and job.get("status") == "cancelled":
        raise AdCancelledError(f"Job {job_id} cancelado; variantes abortadas.")


def _publish(
    broker: ProgressBroker | None,
    event_type: str,
    data: dict[str, Any],
) -> None:
    if broker is not None:
        broker.publish(event_type, data)


def _progress(
    store: ProjectStore,
    broker: ProgressBroker | None,
    job_id: str | None,
    project_id: str,
    value: int,
    max_value: int,
    node: str,
) -> None:
    if not job_id:
        return
    store.update_job(
        job_id, status="running", progress_value=value, progress_max=max_value, current_node=node
    )
    _publish(
        broker,
        "progress",
        {
            "job_id": job_id,
            "project_id": project_id,
            "status": "running",
            "value": value,
            "max": max_value,
            "node": node,
        },
    )


def _resolve_segments(
    store: ProjectStore,
    project_id: str,
    storyboard: AdStoryboard,
    *,
    source_asset_id: str | None,
    chunk_asset_ids: list[str] | None,
) -> list[_Segment]:
    shots = storyboard.shots
    if chunk_asset_ids:
        if len(chunk_asset_ids) != len(shots):
            raise AdPipelineError(
                "chunk_asset_ids debe tener un asset por plano del storyboard "
                f"({len(shots)} planos, {len(chunk_asset_ids)} assets)."
            )
        pairs = [(shot, aid) for shot, aid in zip(shots, chunk_asset_ids, strict=True)]
    else:
        pairs = [(shot, shot.asset_id or source_asset_id) for shot in shots]

    segments: list[_Segment] = []
    for shot, asset_id in pairs:
        if not asset_id:
            raise AdPipelineError(
                f"El plano {shot.index} no tiene asset de video/imagen asignado; "
                "indica product_asset_ids en el brief, source_asset_id o chunk_asset_ids."
            )
        asset = store.get_asset(project_id, asset_id)
        if asset is None:
            raise AdPipelineError(f"Asset no encontrado: {asset_id!r} (plano {shot.index}).")
        if asset.get("kind") not in ("video", "image"):
            raise AdPipelineError(
                f"El asset {asset_id!r} no es video ni imagen (kind={asset.get('kind')!r})."
            )
        path = store.get_asset_path(project_id, asset_id)
        if path is None or not path.is_file():
            raise AdPipelineError(f"El asset {asset_id!r} no tiene archivo físico.")
        media_duration = 0.0
        if asset.get("kind") == "video":
            info = probe_media_info(path)
            media_duration = float(info.get("duration_sec") or 0.0)
            if not info.get("probe_ok"):
                raise AdPipelineError(f"No si pudo sondear el video del asset {asset_id!r}.")
        segments.append(
            _Segment(
                path=path,
                kind=str(asset.get("kind")),
                duration_s=float(shot.duration_s),
                asset_id=str(asset_id),
                media_duration_s=media_duration,
            )
        )
    return segments


def _synth_audio(
    *,
    voiceover: bool,
    voice_id: str,
    music: bool,
    music_prompt: str,
    music_duration_s: float,
    script: AdScript,
    tmp: Path,
    simulated: bool,
) -> tuple[Path | None, Path | None, bool]:
    """Genera voz (TTS) y/o música (music_gen) en `tmp`. Devuelve (voice, music, sim)."""
    voice_path: Path | None = None
    music_path: Path | None = None
    sim = simulated
    if voiceover:
        text = " ".join(s.voiceover for s in script.scenes if s.voiceover).strip()
        if not text:
            raise AdPipelineError("voiceover=1 pero el guion no tiene líneas de voz.")
        voice_path = tmp / "ad_voice.wav"
        if simulated:
            voice_path.write_bytes(_STUB_WAV)
        else:
            from comfy_studio.engines.audio import AudioEngine

            res = AudioEngine().run(
                EngineRunSpec(
                    job={
                        "op": "tts",
                        "output_path": str(voice_path),
                        "text": text[:4000],
                        "voice_id": voice_id,
                    }
                )
            )
            if not res.ok or not voice_path.is_file():
                raise AdPipelineError(
                    f"TTS de voz falló: {res.error or 'error desconocido'}"
                )
            sim = sim or bool(res.simulated)
    if music:
        prompt = music_prompt.strip() or (
            f"cinematic ad music bed, {script.brief.tone} mood, "
            f"{script.language}, no vocals, {script.total_duration_s:.0f}s"
        )
        music_path = tmp / "ad_music.wav"
        if simulated:
            music_path.write_bytes(_STUB_WAV)
        else:
            from comfy_studio.engines.music_gen import MusicGenEngine

            res = MusicGenEngine().run(
                EngineRunSpec(
                    job={
                        "prompt": prompt,
                        "output_path": str(music_path),
                        "duration_s": max(
                            5.0, min(music_duration_s or script.total_duration_s, 60.0)
                        ),
                    }
                )
            )
            if not res.ok or not music_path.is_file():
                raise AdPipelineError(
                    f"music_gen falló: {res.error or 'error desconocido'}"
                )
            sim = sim or bool(res.simulated)
    return voice_path, music_path, sim


def _ffmpeg_variant_cmd(
    segments: list[_Segment],
    *,
    width: int,
    height: int,
    fps: int,
    out_path: Path,
    voice_path: Path | None,
    music_path: Path | None,
    total_duration_s: float,
) -> list[str]:
    inputs: list[str] = []
    filters: list[str] = []
    for i, seg in enumerate(segments):
        if seg.kind == "image":
            inputs += ["-loop", "1", "-t", f"{seg.duration_s:.3f}", "-i", str(seg.path)]
        else:
            dur = seg.duration_s
            if seg.media_duration_s > 0:
                dur = min(dur, seg.media_duration_s)
            inputs += ["-ss", "0", "-t", f"{dur:.3f}", "-i", str(seg.path)]
        filters.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},fps={fps},format=yuv420p[v{i}]"
        )
    n = len(segments)
    concat_in = "".join(f"[v{i}]" for i in range(n))
    filters.append(f"{concat_in}concat=n={n}:v=1:a=0[vout]")

    audio_inputs: list[str] = []
    audio_filters: list[str] = []
    next_idx = n
    if voice_path is not None:
        audio_inputs += ["-i", str(voice_path)]
        audio_filters.append(f"[{next_idx}:a]aresample=44100[a_voice]")
        next_idx += 1
    if music_path is not None:
        audio_inputs += ["-i", str(music_path)]
        audio_filters.append(f"[{next_idx}:a]aresample=44100[a_music]")
        next_idx += 1

    if len(audio_filters) == 2:
        filters.append("[a_voice][a_music]amix=inputs=2:duration=shortest:dropout_transition=0[aout]")
    elif len(audio_filters) == 1:
        label = "a_voice" if voice_path is not None else "a_music"
        filters.append(f"[{label}]anull[aout]")

    filter_complex = ";".join(filters)
    cmd = [
        "ffmpeg_bin_placeholder",
        "-y",
        "-v",
        "error",
        *inputs,
        *audio_inputs,
        "-filter_complex",
        filter_complex,
        "-map",
        "[vout]",
    ]
    if voice_path is not None or music_path is not None:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        cmd += ["-an"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-movflags",
        "+faststart",
        "-t",
        f"{total_duration_s:.3f}",
        str(out_path),
    ]
    return cmd


def _render_variant(
    ffmpeg_bin: str,
    segments: list[_Segment],
    *,
    aspect_ratio: str,
    fps: int,
    out_path: Path,
    voice_path: Path | None,
    music_path: Path | None,
    total_duration_s: float,
    timeout_s: float,
    max_retries: int,
    redactor: SecretRedactor,
) -> None:
    width, height = ASPECT_DIMENSIONS[aspect_ratio]
    cmd = _ffmpeg_variant_cmd(
        segments,
        width=width,
        height=height,
        fps=fps,
        out_path=out_path,
        voice_path=voice_path,
        music_path=music_path,
        total_duration_s=total_duration_s,
    )
    cmd[0] = ffmpeg_bin
    attempts = max_retries + 1
    last_err = ""
    for _attempt in range(1, attempts + 1):
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            last_err = f"ffmpeg excedió el timeout ({timeout_s:.0f}s)"
            continue
        except OSError as exc:
            raise AdPipelineError(
                redactor.redact(f"no se pudo lanzar ffmpeg: {exc}")
            ) from exc
        if res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return
        last_err = redactor.redact((res.stderr or "")[-400:])
        out_path.unlink(missing_ok=True)
    raise AdPipelineError(
        f"render de variante {aspect_ratio} falló tras {attempts} intento(s): {last_err or 'rc!=0'}"
    )


def run_ad_variants(
    store: ProjectStore,
    project_id: str,
    ad_id: str,
    *,
    broker: ProgressBroker | None = None,
    config: StudioConfig | None = None,
    job_id: str | None = None,
    aspect_ratios: list[str] | None = None,
    voiceover: bool = False,
    voice_id: str = "default",
    music: bool = False,
    music_prompt: str = "",
    music_duration_s: float = 0.0,
    source_asset_id: str | None = None,
    chunk_asset_ids: list[str] | None = None,
    fps: int = 24,
    max_retries: int = 0,
    acceptance: dict[str, Any] | None = None,
    simulated: bool = False,
) -> dict[str, Any]:
    """Monta las variantes de formato del anuncio y las evalúa con QA.

    Devuelve un resumen con una entrada por aspect ratio (asset_id, qa, estado).
    Lanza `AdCancelledError` si el job fue cancelado y `AdPipelineError` ante
    cualquier fallo tipado (nunca propaga excepciones de subprocess sin tipar).
    """
    started = time.monotonic()
    if store.get_project(project_id) is None:
        raise AdPipelineError(f"Proyecto no encontrado: {project_id!r}")
    doc = get_ad_doc(store, project_id, ad_id)
    if doc is None:
        raise AdPipelineError(f"Anuncio no encontrado: {ad_id!r}")
    raw_storyboard = doc.get("storyboard")
    if not raw_storyboard:
        raise AdPipelineError(
            f"El anuncio {ad_id!r} no tiene storyboard; llama antes a la op storyboard."
        )
    storyboard = AdStoryboard.model_validate(raw_storyboard)
    script = AdScript.model_validate(doc["script"])

    aspects = [a.strip() for a in (aspect_ratios or list(DEFAULT_AD_ASPECTS)) if a.strip()]
    if not aspects:
        aspects = list(DEFAULT_AD_ASPECTS)
    if len(aspects) > MAX_AD_VARIANTS:
        raise AdPipelineError(f"Máximo {MAX_AD_VARIANTS} variantes por anuncio.")
    for a in aspects:
        if a not in ASPECT_DIMENSIONS:
            raise AdPipelineError(
                f"Aspect ratio no soportado: {a!r} (usa {sorted(ASPECT_DIMENSIONS)})."
            )
    if not (12 <= fps <= 60):
        raise AdPipelineError("fps fuera de rango (12..60).")
    if not (0 <= max_retries <= 2):
        raise AdPipelineError("max_retries fuera de rango (0..2).")
    if len(storyboard.shots) > MAX_EDL_CUTS:
        raise AdPipelineError(f"El storyboard tiene más de {MAX_EDL_CUTS} planos.")

    _check_cancelled(store, job_id)
    segments = _resolve_segments(
        store, project_id, storyboard,
        source_asset_id=source_asset_id, chunk_asset_ids=chunk_asset_ids,
    )
    total_duration_s = round(sum(s.duration_s for s in segments), 3)

    ffmpeg_timeout_s = (
        config.studio_ffmpeg_timeout_s if config is not None else DEFAULT_FFMPEG_TIMEOUT_S
    )
    ffprobe_timeout_s = (
        config.studio_ffprobe_timeout_s if config is not None else DEFAULT_FFPROBE_TIMEOUT_S
    )
    render_timeout = max(120.0, 8.0 * total_duration_s + ffmpeg_timeout_s)
    accept = dict(DEFAULT_AD_ACCEPTANCE)
    if acceptance:
        accept.update({k: v for k, v in acceptance.items() if k in DEFAULT_AD_ACCEPTANCE})

    redactor = SecretRedactor()
    ffmpeg_bin = find_ffmpeg()
    if not simulated and not ffmpeg_bin:
        raise AdPipelineError(
            "ffmpeg no disponible (STUDIO_FFMPEG_BIN o PATH); usa STUDIO_STUB_AD_STUDIO=1"
        )

    if job_id:
        store.update_job(job_id, status="running", progress_value=0, progress_max=len(aspects))
    _publish(
        broker, "ad",
        {"project_id": project_id, "ad_id": ad_id, "job_id": job_id, "event": "start",
         "aspect_ratios": aspects, "shots": len(segments), "simulated": simulated},
    )

    variant_results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="comfy_ad_") as tmp_str:
        tmp = Path(tmp_str)
        voice_path, music_path, audio_sim = _synth_audio(
            voiceover=voiceover, voice_id=voice_id, music=music, music_prompt=music_prompt,
            music_duration_s=music_duration_s, script=script, tmp=tmp, simulated=simulated,
        )
        simulated = simulated or audio_sim

        for idx, aspect in enumerate(aspects):
            _check_cancelled(store, job_id)
            out_path = tmp / f"ad_{ad_id}_{aspect.replace(':', 'x')}.mp4"
            if simulated:
                out_path.write_bytes(_STUB_MP4)
            else:
                _render_variant(
                    ffmpeg_bin or "",
                    segments,
                    aspect_ratio=aspect,
                    fps=fps,
                    out_path=out_path,
                    voice_path=voice_path,
                    music_path=music_path,
                    total_duration_s=total_duration_s,
                    timeout_s=render_timeout,
                    max_retries=max_retries,
                    redactor=redactor,
                )
                info = probe_media_info(out_path, timeout_s=ffprobe_timeout_s)
                if not info.get("probe_ok") or not info.get("has_video"):
                    raise AdPipelineError(
                        f"La variante {aspect} no produjo video verificable (probe_ok="
                        f"{info.get('probe_ok')})."
                    )
                dur = float(info.get("duration_sec") or 0.0)
                if abs(dur - total_duration_s) > max(1.5, total_duration_s * 0.2):
                    raise AdPipelineError(
                        f"La variante {aspect} dura {dur:.2f}s; esperado ~{total_duration_s:.2f}s."
                    )

            asset = store.save_upload(
                project_id,
                filename=f"ad_{ad_id}_{aspect.replace(':', 'x')}.mp4",
                content_type="video/mp4",
                data=out_path.read_bytes(),
                max_upload_bytes=2 * 1024 * 1024 * 1024,
                category="outputs",
                job_id=job_id,
                metadata={
                    "engine": "ad_studio",
                    "ad_id": ad_id,
                    "aspect_ratio": aspect,
                    "shots": [s.asset_id for s in segments],
                    "voiceover": voiceover,
                    "music": music,
                    "simulated": simulated,
                },
            )

            qa_result: dict[str, Any]
            if simulated:
                qa_result = {"passed": True, "level1": None, "simulated": True}
            else:
                from comfy_studio.engines.qa import QaEngine

                qa = QaEngine().run(
                    EngineRunSpec(
                        job={
                            "video_path": str(
                                store.get_asset_path(project_id, asset["id"]) or out_path
                            ),
                            "acceptance": accept,
                        }
                    )
                )
                if not qa.ok:
                    raise AdPipelineError(f"QA de la variante {aspect} falló: {qa.error}")
                qa_result = dict(qa.payload or {})
                qa_result["simulated"] = bool(qa.simulated)

            variant = {
                "aspect_ratio": aspect,
                "asset_id": asset["id"],
                "filename": asset.get("filename"),
                "size_bytes": asset.get("size_bytes"),
                "qa": qa_result,
                "qa_passed": bool(qa_result.get("passed")),
                "simulated": simulated,
                "status": "ok" if qa_result.get("passed") else "needs_review",
            }
            variant_results.append(variant)
            _progress(
                store, broker, job_id, project_id,
                value=idx + 1, max_value=len(aspects), node=f"ad_variant:{aspect}",
            )
            _publish(
                broker, "ad",
                {"project_id": project_id, "ad_id": ad_id, "job_id": job_id,
                 "event": "variant", "aspect_ratio": aspect, "asset_id": asset["id"],
                 "qa_passed": variant["qa_passed"], "simulated": simulated},
            )

    doc["variants"] = variant_results
    _save(store, project_id, doc)

    summary = {
        "ad_id": ad_id,
        "project_id": project_id,
        "job_id": job_id,
        "aspect_ratios": aspects,
        "variant_count": len(variant_results),
        "variants": variant_results,
        "all_qa_passed": all(v["qa_passed"] for v in variant_results),
        "simulated": simulated,
        "duration_s": round(time.monotonic() - started, 3),
        "total_duration_s": total_duration_s,
    }
    if job_id:
        store.update_job(job_id, status="done", result=summary)
    _publish(
        broker, "ad",
        {"project_id": project_id, "ad_id": ad_id, "job_id": job_id, "event": "done",
         "variant_count": len(variant_results), "all_qa_passed": summary["all_qa_passed"],
         "simulated": simulated},
    )
    return summary



