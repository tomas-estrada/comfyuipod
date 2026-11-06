"""Comprensión multimodal del proyecto basada en evidencia (M7, sección 3).

`build_understanding` recorre los assets del proyecto (acotado por `asset_ids`
si se indica) y junta, por cada uno, los hechos verificables disponibles:

- metadatos reales vía ffprobe (`probe_media_info`, `probe_ok` honesto);
- hash sha256 para deduplicación (con tope de tamaño configurable);
- transcripciones persistidas con intervalos temporales y hablantes;
- shots del índice CLIP y detecciones/identidades faciales (si hay índice);
- versiones EDL existentes.

Cada hecho conserva: `asset_id`, `interval_sec`, `method`, `simulated` y
`evidence` (ids de transcripción/segmento/shot/identidad). Los hechos de
backends ML simulados se marcan `simulated: true` con notice. Nada se envía
completo al LLM: el documento es recuperable por tramos (facts acotados).
"""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from comfy_studio import SIMULATED_ML_NOTICE
from comfy_studio.perception.keyframes import probe_media_info

UNDERSTANDING_SCHEMA = "comfy_studio.understanding.v1"

#: tope de tamaño para hashear un archivo (los más grandes se omiten con aviso).
DEFAULT_MAX_HASH_BYTES = int(os.environ.get("STUDIO_BRIEF_MAX_HASH_BYTES", str(512 * 1024 * 1024)))
#: tope de hechos incluidos en el documento (el resto queda resumido en counts).
DEFAULT_MAX_FACTS = int(os.environ.get("STUDIO_BRIEF_MAX_FACTS", "500"))
#: tope de segmentos de transcripción por asset (los más relevantes: con texto).
DEFAULT_MAX_SEGMENTS_PER_ASSET = int(os.environ.get("STUDIO_BRIEF_MAX_SEGMENTS", "100"))


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path, max_bytes: int) -> tuple[str | None, str | None]:
    """Devuelve (sha256, None) o (None, motivo) si se omite el hash."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, "archivo no legible"
    if size > max_bytes:
        return None, f"hash omitido (> {max_bytes} bytes)"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None, "archivo no legible"
    return digest.hexdigest(), None


def _asset_facts_for_transcription(
    tr: dict[str, Any],
    *,
    ml_simulated: bool,
    max_segments: int,
) -> list[dict[str, Any]]:
    facts: list[dict[str, Any]] = []
    segments = tr.get("segments") or []
    for seg in segments[:max_segments]:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = float(seg.get("start") or 0.0)
        end = float(seg.get("end") or start)
        speaker = seg.get("speaker")
        claim = f"Dice: «{text[:300]}»" + (f" ({speaker})" if speaker else "")
        facts.append({
            "kind": "transcript_segment",
            "asset_id": tr.get("asset_id"),
            "interval_sec": [round(start, 3), round(max(end, start), 3)],
            "method": "whisperx",
            "simulated": ml_simulated,
            "evidence": {
                "transcription_id": tr.get("id"),
                "segment_id": seg.get("id"),
                "speaker": speaker,
                "language": tr.get("language"),
                "backend": tr.get("backend"),
                "diarized": bool(tr.get("diarized")),
            },
            "claim": claim,
        })
    return facts


def build_understanding(
    store: Any,
    project_id: str,
    *,
    asset_ids: list[str] | None = None,
    prompt: str = "",
    vector_store: Any | None = None,
    ml_simulated: bool = True,
    max_facts: int = DEFAULT_MAX_FACTS,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
) -> dict[str, Any]:
    """Construye el documento de comprensión del proyecto con evidencia trazable.

    `ml_simulated` marca los hechos de backends ML (CLIP/InsightFace/WhisperX)
    cuando el pod corre en modo simulado; ffprobe es real siempre que haya
    binario (`probe_ok` lo dice por hecho).
    """
    from comfy_studio.creative.brief import parse_prompt

    project = store.get_project(project_id)
    if project is None:
        raise LookupError(f"Proyecto '{project_id}' no encontrado")

    all_assets = store.list_assets(project_id)
    if asset_ids:
        wanted = {a.strip() for a in asset_ids if a and a.strip()}
        assets = [a for a in all_assets if a["id"] in wanted]
        missing = sorted(wanted - {a["id"] for a in assets})
    else:
        assets = all_assets
        missing = []

    facts: list[dict[str, Any]] = []
    asset_entries: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    hash_groups: dict[str, list[str]] = {}

    for asset in assets:
        aid = asset["id"]
        path = store.get_asset_path(project_id, aid)
        entry: dict[str, Any] = {
            "asset_id": aid,
            "kind": asset.get("kind"),
            "category": asset.get("category"),
            "filename": asset.get("filename"),
            "original_filename": asset.get("original_filename"),
            "mime_type": asset.get("mime_type"),
            "size_bytes": asset.get("size_bytes"),
            "metadata": asset.get("metadata") or {},
            "sha256": None,
            "duplicate_of": None,
            "status": "ok",
            "probe": None,
        }
        if path is None or not path.is_file():
            entry["status"] = "missing_file"
            invalid.append({"asset_id": aid, "reason": "archivo físico ausente"})
        else:
            sha, why = _sha256_file(path, max_hash_bytes)
            entry["sha256"] = sha
            if sha is None and why:
                entry["hash_notice"] = why
            if sha is not None:
                if sha in hash_groups:
                    entry["duplicate_of"] = hash_groups[sha][0]
                    hash_groups[sha].append(aid)
                else:
                    hash_groups[sha] = [aid]
            probe = probe_media_info(path)
            entry["probe"] = probe
            if not probe.get("probe_ok"):
                # Archivo presente pero no verificable (ffprobe ausente o archivo
                # dañado): se marca, pero no se excluye de raíz — la selección
                # lo penaliza y la revisión del render detecta el problema real.
                entry["status"] = "probe_unverified"
                invalid.append({
                    "asset_id": aid,
                    "reason": "ffprobe no pudo verificar el archivo (probe_ok=false)",
                })
            facts.append({
                "kind": "media_metadata",
                "asset_id": aid,
                "interval_sec": None,
                "method": "ffprobe",
                "simulated": False,
                "evidence": {"probe_ok": bool(probe.get("probe_ok"))},
                "claim": (
                    f"{asset.get('kind')} {probe.get('width')}x{probe.get('height')}, "
                    f"duración {probe.get('duration_sec')}s, "
                    f"audio={'sí' if probe.get('has_audio') else 'no'}, "
                    f"codec={probe.get('codec_name') or 'desconocido'}"
                    + ("" if probe.get("probe_ok") else " (NO verificado: ffprobe falló)")
                ),
            })
        asset_entries.append(entry)

    # Transcripciones persistidas (por asset, la más reciente primero).
    transcriptions = store.list_transcriptions(project_id)
    by_asset: dict[str, list[dict[str, Any]]] = {}
    for tr in transcriptions:
        aid = tr.get("asset_id")
        if aid:
            by_asset.setdefault(aid, []).append(tr)
    scope_ids = {a["id"] for a in assets}
    tr_count = 0
    for aid, trs in by_asset.items():
        if asset_ids and aid not in scope_ids:
            continue
        tr_count += 1
        latest = trs[0]
        seg_facts = _asset_facts_for_transcription(
            latest, ml_simulated=ml_simulated, max_segments=DEFAULT_MAX_SEGMENTS_PER_ASSET
        )
        facts.extend(seg_facts)
        facts.append({
            "kind": "transcription",
            "asset_id": aid,
            "interval_sec": None,
            "method": "whisperx",
            "simulated": ml_simulated,
            "evidence": {
                "transcription_id": latest.get("id"),
                "language": latest.get("language"),
                "diarized": bool(latest.get("diarized")),
                "speakers": latest.get("speakers") or [],
                "segments_count": len(latest.get("segments") or []),
            },
            "claim": (
                f"Transcripción {latest.get('language')} con "
                f"{len(latest.get('segments') or [])} segmentos"
                + (
                    f", hablantes: {', '.join(latest.get('speakers') or [])}"
                    if latest.get("speakers")
                    else ""
                )
                + (" (simulada)" if ml_simulated else "")
            ),
        })

    # Percepción vectorial (CLIP + rostros) si el índice está disponible.
    perception_available = vector_store is not None
    shot_count = 0
    identities: list[dict[str, Any]] = []
    if perception_available:
        try:
            shots = vector_store.list_clip_shots(project_id)
            for shot in shots:
                if asset_ids and shot.get("source_asset_id") not in scope_ids:
                    continue
                shot_count += 1
                tags = shot.get("visual_tags") or []
                facts.append({
                    "kind": "clip_shot",
                    "asset_id": shot.get("source_asset_id"),
                    "interval_sec": [
                        round(float(shot.get("shot_start_sec") or 0.0), 3),
                        round(float(shot.get("shot_end_sec") or 0.0), 3),
                    ],
                    "method": "clip",
                    "simulated": ml_simulated,
                    "evidence": {
                        "shot_id": shot.get("shot_id"),
                        "keyframe_asset_id": shot.get("keyframe_asset_id"),
                        "timestamp_sec": shot.get("timestamp_sec"),
                        "caption": shot.get("caption"),
                    },
                    "claim": (
                        f"Plano en t={shot.get('timestamp_sec')}s"
                        + (f" con etiquetas {', '.join(tags[:8])}" if tags else "")
                        + (f" — {shot.get('caption')}" if shot.get("caption") else "")
                        + (" (embedding simulado)" if ml_simulated else "")
                    ),
                })
        except Exception:
            perception_available = False
        try:
            identities = store.list_identities(project_id)
            for ident in identities:
                if not isinstance(ident, dict):
                    continue
                facts.append({
                    "kind": "face_identity",
                    "asset_id": ident.get("reference_asset_id"),
                    "interval_sec": None,
                    "method": "insightface",
                    "simulated": ml_simulated,
                    "evidence": {"identity_id": ident.get("id"), "name": ident.get("name")},
                    "claim": (
                        f"Identidad registrada: {ident.get('name')}"
                        + (" (simulada)" if ml_simulated else "")
                    ),
                })
        except Exception:
            pass

    # EDL existentes.
    edls = store.list_edl_versions(project_id)
    if edls:
        latest = edls[0]
        facts.append({
            "kind": "edl",
            "asset_id": latest.get("output_asset_id"),
            "interval_sec": None,
            "method": "edl",
            "simulated": False,
            "evidence": {"edl_id": latest.get("id"), "version": latest.get("version")},
            "claim": (
                f"EDL v{latest.get('version')} con "
                f"{len((latest.get('edl') or {}).get('cuts') or [])} cortes"
                + (
                    f", renderizado como asset {latest.get('output_asset_id')}"
                    if latest.get("output_asset_id")
                    else " (sin render)"
                )
            ),
        })

    facts_total = len(facts)
    truncated = facts_total > max_facts
    facts = facts[:max_facts]
    for i, fact in enumerate(facts):
        fact["id"] = f"fact-{i + 1:04d}"

    duplicates = [
        {"sha256": sha, "asset_ids": ids}
        for sha, ids in hash_groups.items()
        if len(ids) > 1
    ]

    limitations: list[str] = []
    if ml_simulated:
        limitations.append(
            f"Backends ML en modo simulado: {SIMULATED_ML_NOTICE}"
        )
    if invalid:
        limitations.append(
            f"{len(invalid)} asset(s) no verificables (ver `invalid`); "
            "el proyecto continúa sin ellos."
        )
    if missing:
        limitations.append(f"asset_ids pedidos no encontrados: {', '.join(missing)}")
    if truncated:
        limitations.append(f"Hechos truncados a {max_facts} (ver counts.facts_total).")

    return {
        "schema": UNDERSTANDING_SCHEMA,
        "project_id": project_id,
        "generated_at": _utc_now_iso(),
        "scope": {"asset_ids": sorted(scope_ids) if asset_ids else "all", "prompt": prompt},
        "assets": asset_entries,
        "facts": facts,
        "duplicates": duplicates,
        "invalid": invalid,
        "counts": {
            "assets": len(asset_entries),
            "facts": len(facts),
            "facts_total": facts_total,
            "transcriptions": tr_count,
            "shots": shot_count,
            "identities": len(identities),
            "edl_versions": len(edls),
            "duplicates": sum(len(d["asset_ids"]) - 1 for d in duplicates),
        },
        "perception_available": perception_available,
        "intent": parse_prompt(prompt) if prompt else None,
        "limitations": limitations,
    }
