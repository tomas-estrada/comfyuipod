"""Inspiración activa → receta editable (plan Ultra, W06 / A09).

Análisis local sobre la biblioteca propia; conectores opcionales/blocked, nunca
de pago. Distingue observado/inferido/adaptado; no promete viralidad. Puro CPU.
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
from pathlib import Path

import pytest

from comfy_studio.inspiration import (
    ConnectorStatus,
    Cut,
    Origin,
    ReferenceAnalysis,
    _beat_candidates_from_pcm,
    analyze_reference_asset,
    assert_no_paid_connector,
    connector_catalog,
    derive_recipe,
)
from comfy_studio.media_bins import find_ffmpeg
from comfy_studio.projects import ProjectStore


def _analysis() -> ReferenceAnalysis:
    return ReferenceAnalysis(
        reference_id="ref1",
        cuts=[Cut(0.0, 2.0), Cut(2.0, 3.5), Cut(3.5, 6.0)],
        beats=[0.0, 0.5, 1.0, 1.5, 2.0],
        titles=[{"text": "ATARDECER", "start_s": 0.2, "origin": "observed"}],
        camera_moves=["pan", "static", "zoom"],
        source="own_library",
    )


def test_analysis_computes_beat_tempo() -> None:
    a = _analysis()
    assert a.beat_interval_s() == 0.5  # beats cada 0.5 s


def test_derive_recipe_maps_cuts_titles_and_beats() -> None:
    recipe = derive_recipe(_analysis())
    kinds = [s.kind for s in recipe.steps]
    assert kinds.count("shot") == 3  # un shot por corte
    assert "beat_sync" in kinds       # tempo inferido
    assert "title" in kinds           # título OCR observado
    # Los shots son observados; el beat_sync es inferido.
    assert all(s.origin is Origin.OBSERVED for s in recipe.steps if s.kind == "shot")
    assert all(s.origin is Origin.INFERRED for s in recipe.steps if s.kind == "beat_sync")


def test_recipe_distinguishes_observed_inferred_adapted() -> None:
    """A09: el análisis distingue qué observó, infirió y adaptó al brief."""
    recipe = derive_recipe(_analysis(), brief_shots=5)
    summary = recipe.to_dict()["origin_summary"]
    assert summary["observed"] >= 3   # 3 shots + título
    assert summary["inferred"] >= 1   # beat_sync
    assert summary["adapted"] == 1    # adaptación de 3 -> 5 shots
    # La adaptación deja nota trazable.
    assert "adaptado" in recipe.notes


def test_recipe_does_not_promise_virality() -> None:
    recipe = derive_recipe(_analysis())
    assert "no predice viralidad" in recipe.to_dict()["disclaimer"]


def test_connector_without_credentials_is_blocked_not_invented() -> None:
    """A09: un conector sin credenciales queda blocked, no inventa datos."""
    cats = {c.name: c for c in connector_catalog(env={})}
    assert cats["own_library"].status is ConnectorStatus.AVAILABLE
    assert cats["wikimedia_commons"].status is ConnectorStatus.BLOCKED
    assert not cats["wikimedia_commons"].is_usable()


def test_optional_connector_enabled_by_env() -> None:
    cats = {c.name: c for c in connector_catalog(env={"STUDIO_WIKIMEDIA_ENABLED": "1"})}
    assert cats["wikimedia_commons"].status is ConnectorStatus.AVAILABLE


def test_paid_connector_is_never_invoked() -> None:
    """A09: no se invoca cloud/compra ni cuenta no concedida."""
    cats = {c.name: c for c in connector_catalog(env={})}
    heygen = cats["heygen_reverse"]
    assert heygen.status is ConnectorStatus.FORBIDDEN_PAID
    with pytest.raises(RuntimeError, match="no se invoca"):
        assert_no_paid_connector(heygen)


def test_offline_mode_works_on_own_library() -> None:
    """Modo sin red: el análisis funciona solo con la biblioteca propia."""
    a = ReferenceAnalysis(reference_id="local", cuts=[Cut(0.0, 1.0)], source="own_library")
    recipe = derive_recipe(a)
    assert recipe.reference_id == "local"
    assert len([s for s in recipe.steps if s.kind == "shot"]) == 1
    # No requirió ningún conector externo.
    assert a.source == "own_library"


def test_beat_interval_s_devuelve_none_con_menos_de_dos_beats() -> None:
    """Guarda: con 1 beat o 0 beats no hay tempo, devuelve None."""
    a = ReferenceAnalysis(reference_id="ref", beats=[0.0])
    assert a.beat_interval_s() is None
    a.beats = []
    assert a.beat_interval_s() is None


def test_derive_recipe_sin_tempo_no_incluye_beat_sync() -> None:
    """Sin >=2 beats no hay intervalo, por tanto no hay paso beat_sync."""
    a = ReferenceAnalysis(
        reference_id="ref",
        cuts=[Cut(0.0, 2.0), Cut(2.0, 3.5)],
        beats=[0.0],  # <2 beats -> beat_interval_s() -> None
        titles=[],
        camera_moves=["pan", "static"],
    )
    recipe = derive_recipe(a)
    kinds = [s.kind for s in recipe.steps]
    assert "beat_sync" not in kinds
    assert kinds.count("shot") == 2


def test_missing_camera_evidence_is_not_reported_as_static() -> None:
    recipe = derive_recipe(ReferenceAnalysis(reference_id="ref", cuts=[Cut(0.0, 1.0)]))
    shot = next(step for step in recipe.steps if step.kind == "shot")
    assert shot.detail["camera_move"] == "unknown"


def test_derived_recipe_preserves_inferred_cut_provenance() -> None:
    recipe = derive_recipe(
        ReferenceAnalysis(reference_id="ref", cuts=[Cut(0.0, 1.0, Origin.INFERRED)])
    )
    shot = next(step for step in recipe.steps if step.kind == "shot")
    assert shot.origin is Origin.INFERRED


def test_local_pcm_beat_candidates_track_strong_audio_onsets() -> None:
    # Quiet baseline with two short amplitude rises, separated by >180 ms.
    sample_rate = 8000
    samples: list[int] = []
    for i in range(400):
        amplitude = 100 if (i // 10) % 25 not in (5, 15) else 12000
        samples.extend([amplitude] * 160)
    pcm = b"".join(struct.pack("<h", value) for value in samples)
    beats = _beat_candidates_from_pcm(pcm, sample_rate)
    assert len(beats) >= 2
    assert all(0 <= beat < 10 for beat in beats)


def test_reference_asset_analysis_extracts_real_local_video(tmp_path: Path, client) -> None:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for local reference extraction")
    video_path = tmp_path / "reference.mp4"
    generated = subprocess.run(
        [
            ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=64x48:d=0.5:r=10", "-f", "lavfi", "-i",
            "color=c=red:s=64x48:d=0.5:r=10", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1", "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
            "-shortest", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(video_path),
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    assert generated.returncode == 0, generated.stderr.decode(errors="replace")
    store = ProjectStore(tmp_path / "studio")
    project = store.create_project(name="reference analysis")
    asset = store.save_upload(
        project["id"], filename="reference.mp4", content_type="video/mp4",
        data=video_path.read_bytes(), max_upload_bytes=10_000_000,
    )

    analysis = analyze_reference_asset(store, project["id"], asset["id"])

    assert analysis.reference_id == asset["id"]
    assert analysis.media_sha256 == hashlib.sha256(video_path.read_bytes()).hexdigest()
    assert analysis.media_duration_s == pytest.approx(1.0, abs=0.15)
    assert len(analysis.cuts) >= 2
    assert all(cut.origin is Origin.OBSERVED for cut in analysis.cuts)
    assert analysis.cuts[0].start_s == 0
    assert analysis.source == "own_library"
    assert analysis.camera_moves == ["unknown"] * len(analysis.cuts)
    assert analysis.limitations

    api_project = client.post("/studio/api/projects", json={"name": "API reference"})
    assert api_project.status_code in (200, 201), api_project.text
    api_project_id = api_project.json()["id"]
    uploaded = client.post(
        f"/studio/api/projects/{api_project_id}/assets",
        files={"file": ("api-reference.mp4", video_path.read_bytes(), "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    response = client.post(
        f"/studio/api/projects/{api_project_id}/assets/{uploaded.json()['id']}"
        "/inspiration/analyze",
        params={"brief_shots": 3},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["analysis"]["cuts"]) >= 2
    assert payload["analysis"]["cuts"][0]["origin"] == "observed"
    assert payload["analysis"]["media_sha256"] == hashlib.sha256(
        video_path.read_bytes()
    ).hexdigest()
    assert payload["recipe"]["origin_summary"]["adapted"] == 1
    assert payload["simulated"] is False
    assert payload["network_used"] is False
    missing = client.post(
        f"/studio/api/projects/{api_project_id}/assets/missing/inspiration/analyze"
    )
    assert missing.status_code == 404
