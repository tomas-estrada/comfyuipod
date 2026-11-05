"""Tests del montaje asistido desde brief (M7, `comfy_studio/montage`).

Cubre:
- Unidad: parseo del brief (términos/personas/rango temporal), fusión de
  candidatos por IoU y empaquetado de cortes con duración objetivo.
- Integración (ffmpeg + sqlite-vec reales): `POST .../montage/from-brief` sobre un
  proyecto con vídeo real de 3 escenas, índice CLIP y transcripción con
  `script_segments`; cada corte debe traer evidencia trazable y timecodes
  verificados con ffprobe.
- Medición: precisión del montaje frente al brief (duración relevante
  seleccionada) comparada con la línea base de `edl/highlights` (todos los
  segmentos), sobre el mismo fixture.
- Render del borrador con `render=true`: MP4 real verificado con ffprobe.
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.montage.brief import (
    merge_candidates,
    parse_brief,
    select_cuts,
    tokenize_terms,
)


def _make_rgb_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    r_bg, g_bg, b_bg = rgb
    rows: list[bytes] = []
    for _y in range(height):
        row = bytearray([0])
        for _x in range(width):
            row.extend((r_bg & 0xFF, g_bg & 0xFF, b_bg & 0xFF))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(raw, level=6)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def _synthesize_scene_video_mp4(out_path: Path) -> None:
    """MP4 real de 6.0s con 3 escenas de 2.0s (verde, rojo, azul) vía ffmpeg."""
    ffmpeg_bin = shutil.which("ffmpeg")
    assert ffmpeg_bin is not None, "ffmpeg debe estar instalado para los tests de M7"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=c=#10b981:s=320x180:d=2:r=24",
        "-f",
        "lavfi",
        "-i",
        "color=c=#ef4444:s=320x180:d=2:r=24",
        "-f",
        "lavfi",
        "-i",
        "color=c=#3b82f6:s=320x180:d=2:r=24",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[outv]",
        "-map",
        "[outv]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    proc = subprocess.run(cmd, check=False, capture_output=True, timeout=60.0)
    assert proc.returncode == 0, f"Fallo al sintetizar MP4 con ffmpeg: {proc.stderr.decode()}"


SCRIPT_SEGMENTS = [
    {
        "start": 0.0,
        "end": 2.0,
        "text": "paseamos por el bosque verde y disfrutamos la naturaleza",
        "speaker": "SPEAKER_00",
    },
    {
        "start": 2.0,
        "end": 4.0,
        "text": "vimos el incendio y el fuego rojo en la montaña",
        "speaker": "SPEAKER_01",
    },
    {
        "start": 4.0,
        "end": 6.0,
        "text": "luego llegamos a la playa y al mar azul del caribe",
        "speaker": "SPEAKER_00",
    },
]

# Ground truth del brief "incendio y fuego": solo el segmento 2.0-4.0 es relevante.
RELEVANT_SPAN = (2.0, 4.0)


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _relevant_fraction(cuts: list[dict], span: tuple[float, float]) -> float:
    total = sum(float(c["out_sec"]) - float(c["in_sec"]) for c in cuts)
    if total <= 0:
        return 0.0
    rel = sum(_overlap(float(c["in_sec"]), float(c["out_sec"]), span[0], span[1]) for c in cuts)
    return rel / total


def _make_project_with_media(client: TestClient, tmp_path: Path) -> tuple[str, str]:
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Montaje M7", "aspect_ratio": "16:9"},
    ).json()
    project_id = proj["id"]
    video_file = tmp_path / "escenas_3colores.mp4"
    _synthesize_scene_video_mp4(video_file)
    up_resp = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("escenas_3colores.mp4", video_file.read_bytes(), "video/mp4")},
        data={"category": "uploads"},
    )
    assert up_resp.status_code == 201
    video_asset_id = up_resp.json()["id"]
    idx_resp = client.post(
        f"/studio/api/projects/{project_id}/perception/index",
        json={"asset_id": video_asset_id, "interval_sec": 2.0, "detect_faces": False},
    )
    assert idx_resp.status_code == 200, idx_resp.text
    tr_resp = client.post(
        f"/studio/api/projects/{project_id}/transcribe",
        json={"asset_id": video_asset_id, "language": "es", "script_segments": SCRIPT_SEGMENTS},
    )
    assert tr_resp.status_code == 200, tr_resp.text
    return project_id, video_asset_id


# ---------------------------------------------------------------------------
# Unidad: parseo del brief, fusión y empaquetado
# ---------------------------------------------------------------------------


def test_parse_brief_extracts_terms_persons_and_time_range() -> None:
    parsed = parse_brief(
        "Quiero a Ana hablando del incendio, del segundo 2 al 4 por favor",
        ["Ana", "Carlos"],
    )
    assert "incendio" in parsed["terms"]
    assert "segundo" not in parsed["terms"]
    assert parsed["persons"] == ["Ana"]
    assert parsed["time_range_sec"] == [2.0, 4.0]


def test_tokenize_terms_normalizes_accents_and_stopwords() -> None:
    terms = tokenize_terms("El mar azul y la playa, ¡Bien!")
    assert "mar" in terms
    assert "playa" in terms
    assert "azul" in terms
    assert "el" not in terms and "la" not in terms and "y" not in terms


def test_merge_candidates_fuses_overlapping_evidence_same_asset() -> None:
    candidates = [
        {
            "asset_id": "a1",
            "in_sec": 2.0,
            "out_sec": 4.0,
            "score": 1.0,
            "evidence": [{"mode": "transcript", "transcription_id": "tr-1"}],
        },
        {
            "asset_id": "a1",
            "in_sec": 2.5,
            "out_sec": 4.5,
            "score": 0.8,
            "evidence": [{"mode": "visual", "shot_id": "sh-1"}],
        },
        {
            "asset_id": "a1",
            "in_sec": 5.0,
            "out_sec": 6.0,
            "score": 0.5,
            "evidence": [{"mode": "visual", "shot_id": "sh-2"}],
        },
    ]
    merged = merge_candidates(candidates)
    assert len(merged) == 2
    top = merged[0]
    assert top["in_sec"] == 2.0 and top["out_sec"] == 4.5
    assert {e["mode"] for e in top["evidence"]} == {"transcript", "visual"}
    assert top["score"] > 1.0 - 1e-9 or top["score"] <= 1.0
    assert top["score"] >= 1.0


def test_select_cuts_respects_target_duration_and_verifies_timecodes() -> None:
    candidates = [
        {
            "asset_id": "a1",
            "in_sec": 2.0,
            "out_sec": 4.0,
            "score": 1.0,
            "evidence": [{"mode": "transcript", "timecode_verified": True}],
        },
        {
            "asset_id": "a1",
            "in_sec": 0.0,
            "out_sec": 2.0,
            "score": 0.6,
            "evidence": [{"mode": "visual", "timecode_verified": True}],
        },
        {
            "asset_id": "a1",
            "in_sec": 4.0,
            "out_sec": 6.0,
            "score": 0.4,
            "evidence": [{"mode": "visual", "timecode_verified": True}],
        },
    ]
    durations = {"a1": {"duration_sec": 6.0, "probe_ok": True}}
    cuts, report = select_cuts(
        candidates,
        durations,
        target_duration_sec=3.0,
        max_cuts=10,
        min_segment_sec=1.0,
        max_segment_sec=30.0,
        time_range=None,
    )
    assert report["selected"] == 2
    assert report["trimmed_to_duration"] == 1
    total = sum(c["out_sec"] - c["in_sec"] for c in cuts)
    assert abs(total - 3.0) < 1e-6
    spans = {(c["in_sec"], c["out_sec"]) for c in cuts}
    assert (2.0, 4.0) in spans
    assert (0.0, 1.0) in spans


def test_select_cuts_drops_segments_beyond_probed_duration() -> None:
    candidates = [
        {
            "asset_id": "a1",
            "in_sec": 5.0,
            "out_sec": 9.0,
            "score": 0.9,
            "evidence": [{"mode": "visual"}],
        }
    ]
    durations = {"a1": {"duration_sec": 6.0, "probe_ok": True}}
    cuts, report = select_cuts(
        candidates,
        durations,
        target_duration_sec=None,
        max_cuts=10,
        min_segment_sec=1.0,
        max_segment_sec=30.0,
        time_range=None,
    )
    assert len(cuts) == 1
    assert cuts[0]["out_sec"] == 6.0
    assert report["clamped_by_probe"] == 1
    assert cuts[0]["evidence"][0]["timecode_verified"] is True


# ---------------------------------------------------------------------------
# Integración: endpoint con evidencia verificable y comparación con baseline
# ---------------------------------------------------------------------------


def test_montage_from_brief_end_to_end_evidence_and_baseline_comparison(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id, video_asset_id = _make_project_with_media(client, tmp_path)

    brief = "quiero los momentos del incendio y el fuego"
    resp = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={
            "brief": brief,
            "target_duration_sec": 2.5,
            "aspect_ratio": "16:9",
            "render": False,
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["status"] == "planned"
    assert plan["render"] is None
    assert plan["cuts"], "el brief debe producir al menos un corte"
    assert plan["evidence_coverage"] == 1.0
    assert plan["cuts_timecode_verified"] == len(plan["cuts"])
    assert plan["simulated"] is True
    assert plan["simulation_notice"]

    for cut in plan["cuts"]:
        assert cut["asset_id"] == video_asset_id
        assert 0.0 <= cut["in_sec"] < cut["out_sec"] <= 6.0
        assert cut["evidence"], "cada corte debe traer evidencia trazable"
        modes = {e["mode"] for e in cut["evidence"]}
        assert modes & {"transcript", "visual", "identity"}
        for ev in cut["evidence"]:
            assert ev["timecode_verified"] is True
            assert ev["simulated"] is True
            if ev["mode"] == "transcript":
                assert ev["transcription_id"].startswith("tr-")
                assert ev["matched_terms"]
                assert "incendio" in ev["matched_terms"] or "fuego" in ev["matched_terms"]
            if ev["mode"] == "visual":
                assert ev["shot_id"] and ev["keyframe_asset_id"]

    montage_precision = _relevant_fraction(plan["cuts"], RELEVANT_SPAN)
    assert montage_precision == 1.0, (
        f"con duración objetivo 2.5s el montaje debe concentrarse en el tramo "
        f"relevante {RELEVANT_SPAN}; cortes={plan['cuts']}"
    )

    edl_list = client.get(f"/studio/api/projects/{project_id}/edl").json()
    latest = edl_list["latest"]
    assert latest["edl"]["kind"] == "montage"
    assert latest["edl"]["schema"] == "comfy_studio.edl.v1"
    assert latest["edl"]["montage"]["brief"] == brief
    assert len(latest["edl"]["cuts"]) == len(plan["cuts"])
    assert latest["version"] == plan["version"]

    baseline = client.post(
        f"/studio/api/projects/{project_id}/edl/highlights",
        json={"asset_id": video_asset_id, "remove_fillers": True},
    )
    assert baseline.status_code == 200, baseline.text
    baseline_cuts = baseline.json()["edl"]["cuts"]
    assert len(baseline_cuts) >= 3
    baseline_precision = _relevant_fraction(baseline_cuts, RELEVANT_SPAN)
    assert baseline_precision < 0.5
    assert montage_precision > baseline_precision, (
        f"el montaje desde brief ({montage_precision:.3f}) debe superar a la línea "
        f"base de highlights ({baseline_precision:.3f}) en precisión frente al brief"
    )


def test_montage_from_brief_render_draft_verified_by_ffprobe(
    client: TestClient,
    tmp_path: Path,
) -> None:
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe no disponible en este entorno")
    project_id, _video_asset_id = _make_project_with_media(client, tmp_path)

    resp = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={
            "brief": "quiero los momentos del incendio y el fuego",
            "target_duration_sec": 5.0,
            "aspect_ratio": "16:9",
            "render": True,
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert plan["status"] == "planned_and_rendered"
    render = plan["render"]
    assert render["status"] == "built"
    assert render["verification"]["probe_ok"] is True
    assert render["verification"]["verified"] is True
    expected = render["verification"]["expected_duration_sec"]
    actual = render["verification"]["actual_duration_sec"]
    assert abs(actual - expected) <= 0.5
    assert abs(expected - 5.0) <= 0.01
    assert render["verification"]["width"] == 1280
    assert render["verification"]["height"] == 720

    out_asset = render["output_asset"]
    file_resp = client.get(f"/studio/api/projects/{project_id}/assets/{out_asset['id']}/file")
    assert file_resp.status_code == 200
    assert b"ftyp" in file_resp.content[:16]

    edl_list = client.get(f"/studio/api/projects/{project_id}/edl").json()
    assert edl_list["latest"]["output_asset_id"] == out_asset["id"]


def test_montage_from_brief_detects_person_mention_and_time_range(
    client: TestClient,
    tmp_path: Path,
) -> None:
    project_id, video_asset_id = _make_project_with_media(client, tmp_path)

    png = _make_rgb_png(64, 64, (40, 180, 90))
    up = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("ana.png", png, "image/png")},
        data={"category": "uploads"},
    )
    assert up.status_code == 201
    ident = client.post(
        f"/studio/api/projects/{project_id}/identities",
        json={"name": "Ana", "reference_asset_id": up.json()["id"]},
    )
    assert ident.status_code == 201, ident.text

    resp = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={
            "brief": "quiero a Ana paseando por el bosque verde del segundo 0 al 2",
            "time_range_sec": [0.0, 2.0],
            "asset_ids": [video_asset_id],
            "target_duration_sec": 2.0,
        },
    )
    assert resp.status_code == 200, resp.text
    plan = resp.json()
    assert "Ana" in plan["persons"]
    assert plan["time_range_sec"] == [0.0, 2.0]
    assert plan["cuts"]
    for cut in plan["cuts"]:
        assert 0.0 <= cut["in_sec"] and cut["out_sec"] <= 2.0 + 1e-6
    assert "identity_shots" in plan["retrieval"]


def test_montage_from_brief_edge_cases(client: TestClient, tmp_path: Path) -> None:
    project_id, video_asset_id = _make_project_with_media(client, tmp_path)

    missing = client.post(
        "/studio/api/projects/proj-no-existe/montage/from-brief",
        json={"brief": "incendio y fuego"},
    )
    assert missing.status_code == 404

    short = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={"brief": "no"},
    )
    assert short.status_code == 422

    bad_asset = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={"brief": "incendio y fuego", "asset_ids": ["asset-fantasma"]},
    )
    assert bad_asset.status_code == 400
    assert "no encontrados" in bad_asset.json()["detail"].lower()

    no_match = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={
            "brief": "zanzibar helicoptero quantum estrategiaiscal",
            "time_range_sec": [0.0, 0.5],
        },
    )
    assert no_match.status_code == 400

    bad_range = client.post(
        f"/studio/api/projects/{project_id}/montage/from-brief",
        json={"brief": "incendio y fuego", "time_range_sec": [4.0, 2.0]},
    )
    assert bad_range.status_code == 422


# ---------------------------------------------------------------------------
# Panel (M7): la vista EDL expone la UI del montaje desde brief
# ---------------------------------------------------------------------------


def test_panel_expone_ui_montaje_desde_brief_sin_inline_handlers() -> None:
    import re

    repo_root = Path(__file__).resolve().parents[2]
    index_html = (repo_root / "panel" / "index.html").read_text(encoding="utf-8")
    app_js = (repo_root / "panel" / "app.js").read_text(encoding="utf-8")
    style_css = (repo_root / "panel" / "style.css").read_text(encoding="utf-8")

    for marker in (
        'id="montage-brief-form"',
        'id="montage-brief-input"',
        'id="montage-target-duration"',
        'id="montage-aspect-select"',
        'id="montage-render-checkbox"',
        'id="montage-status"',
        'id="montage-summary"',
        'id="montage-cuts-list"',
    ):
        assert marker in index_html, f"falta {marker} en panel/index.html"

    for marker in (
        "/montage/from-brief",
        "function runMontageFromBrief",
        "function renderMontagePlan",
        "function renderMontageEvidenceChip",
        'addEventListener("montage"',
        "evidence-chip",
    ):
        assert marker in app_js, f"falta {marker} en panel/app.js"

    assert ".evidence-chip" in style_css

    inline_styles = re.findall(r'style="[^"]*"', app_js)
    allowed = [s for s in inline_styles if s.startswith('style="width:${')]
    assert inline_styles == allowed, f"app.js usa style inline no permitidos: {inline_styles}"
    assert not re.search(r"\son(click|load|error|mouseover)=", app_js, re.I)
