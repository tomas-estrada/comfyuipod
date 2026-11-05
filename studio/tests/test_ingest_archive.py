"""Archivo semántico e ingesta (plan Ultra, W05 / acceptance A08).

Fixture de entradas mixtas (vídeo, audio, imagen, duplicados y un corrupto),
no 100 copias del mismo PNG. Verifica dedup, aislamiento de corruptos, segmentos
con evidencia y backpressure. Puro CPU, sin modelos.
"""

from __future__ import annotations

from pathlib import Path

from comfy_studio.ingest import (
    AssetStatus,
    MediaKind,
    classify,
    content_hash,
    ingest_assets,
)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _mixed_fixture(root: Path) -> list[Path]:
    """Entradas heterogéneas y realistas, con duplicados y un corrupto."""
    paths: list[Path] = []
    # 40 vídeos únicos.
    for i in range(40):
        paths.append(_write(root / f"clip_{i}.mp4", f"video-bytes-{i}".encode() * 4))
    # 30 imágenes únicas.
    for i in range(30):
        paths.append(_write(root / f"img_{i}.png", f"png-bytes-{i}".encode() * 4))
    # 15 audios únicos.
    for i in range(15):
        paths.append(_write(root / f"voz_{i}.wav", f"wav-bytes-{i}".encode() * 4))
    # 10 DUPLICADOS exactos de clip_0.mp4 (mismo contenido).
    dup_payload = b"video-bytes-0" * 4
    for i in range(10):
        paths.append(_write(root / f"copia_clip0_{i}.mp4", dup_payload))
    # 1 corrupto (vacío).
    paths.append(_write(root / "roto.mp4", b""))
    # 1 no soportado.
    paths.append(_write(root / "notas.xyz", b"contenido raro"))
    return paths


def test_classify_by_extension() -> None:
    assert classify(Path("a.mp4")) is MediaKind.VIDEO
    assert classify(Path("a.wav")) is MediaKind.AUDIO
    assert classify(Path("a.png")) is MediaKind.IMAGE
    assert classify(Path("a.xyz")) is MediaKind.UNKNOWN


def test_mixed_batch_dedups_isolates_corrupt_and_indexes(tmp_path: Path) -> None:
    paths = _mixed_fixture(tmp_path)
    assert len(paths) == 97  # 40+30+15+10 dup +1 corrupto +1 no soportado

    report = ingest_assets(paths, project_id="p1", weights_available=True)
    counts = report.counts()

    # 85 únicos indexados (40 vid + 30 img + 15 audio).
    assert counts.get("indexed") == 85
    # 10 duplicados detectados (no multiplican índice).
    assert counts.get("duplicate") == 10
    # El corrupto está aislado, no rompió el lote.
    assert counts.get("corrupt") == 1
    assert counts.get("unsupported") == 1


def test_duplicates_point_to_the_canonical_asset(tmp_path: Path) -> None:
    payload = b"identico" * 100
    a = _write(tmp_path / "a.mp4", payload)
    b = _write(tmp_path / "b.mp4", payload)  # mismo contenido
    report = ingest_assets([a, b], weights_available=True)
    canonical = report.by_status(AssetStatus.INDEXED)
    dups = report.by_status(AssetStatus.DUPLICATE)
    assert len(canonical) == 1
    assert len(dups) == 1
    # El duplicado apunta al canónico (no se reprocesa).
    assert dups[0].duplicate_of == canonical[0].asset_id
    # Mismo hash de contenido.
    assert canonical[0].content_sha256 == content_hash(a)


def test_corrupt_asset_yields_diagnostic_without_breaking_valid_ones(tmp_path: Path) -> None:
    good = _write(tmp_path / "ok.png", b"png-real" * 10)
    bad = _write(tmp_path / "bad.png", b"")  # vacío
    report = ingest_assets([bad, good], weights_available=True)
    assert len(report.by_status(AssetStatus.INDEXED)) == 1  # el bueno sigue
    corrupt = report.by_status(AssetStatus.CORRUPT)
    assert len(corrupt) == 1
    assert corrupt[0].error  # diagnóstico presente


def test_segments_carry_evidence_and_time_scope(tmp_path: Path) -> None:
    """Un vídeo indexado planifica segmentos con evidencia (asr/keyframe)."""
    vid = _write(tmp_path / "v.mp4", b"video" * 50)
    report = ingest_assets([vid], weights_available=True)
    asset = report.by_status(AssetStatus.INDEXED)[0]
    evidence_kinds = {s["evidence"] for s in asset.segments}
    assert "asr" in evidence_kinds and "keyframe" in evidence_kinds
    assert all(s["status"] == "ready" for s in asset.segments)


def test_without_weights_status_is_pending_not_fake(tmp_path: Path) -> None:
    """Sin pesos: estado pending_weights honesto, no un embedding inventado."""
    vid = _write(tmp_path / "v.mp4", b"video" * 50)
    report = ingest_assets([vid], weights_available=False)
    asset = report.assets[0]
    assert asset.status == AssetStatus.PENDING_WEIGHTS
    assert all(s["status"] == "pending_weights" for s in asset.segments)


def test_video_declares_rotation_and_vfr_transforms(tmp_path: Path) -> None:
    """La ingesta DECLARA rotación y VFR→timebase (las aplica el motor de media)."""
    vid = _write(tmp_path / "mobile.mp4", b"rotated-vfr" * 20)
    report = ingest_assets([vid], weights_available=True)
    tr = report.assets[0].transforms
    assert tr.get("normalize_timebase") == "project_cfr"
    assert tr.get("apply_rotation_metadata") is True


def test_backpressure_small_inflight_processes_all(tmp_path: Path) -> None:
    """Un inflight pequeño procesa todo el lote igual (en bloques)."""
    paths = [_write(tmp_path / f"f_{i}.png", f"x{i}".encode() * 8) for i in range(50)]
    report = ingest_assets(paths, weights_available=True, inflight=4)
    assert len(report.by_status(AssetStatus.INDEXED)) == 50
