"""Rutas del plan de sonido por escena por proyecto (C25/A20, CPU)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.audio import AudioProcessError
from comfy_studio.config import StudioConfig
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

FF_REQUIRED = pytest.mark.skipif(
    FFMPEG is None or FFPROBE is None,
    reason="ffmpeg/ffprobe requeridos para el render real de sonido por escena",
)


@pytest.fixture()
def scene_client(tmp_path: Path) -> TestClient:
    """App con presupuesto de salida suficiente para stems de 15 s (C25)."""
    config = StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=tmp_path / "studio_data",
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-test-pass-42",
        require_auth=True,
        max_upload_bytes=24 * 1024 * 1024,
        sse_heartbeat_seconds=0.05,
    )
    transport = httpx.ASGITransport(
        app=create_stub_comfyui_app(ComfyUIStubState(healthy=True))
    )
    app = create_app(config=config, comfyui_transport=transport)
    tc = TestClient(app)
    tc.auth = ("admin", "changeme-test-pass-42")
    return tc


def _make_sample(path: Path, *, freq: int = 440, seconds: float = 0.4) -> None:
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency={freq}:duration={seconds}",
            "-af", "volume=4,aformat=sample_rates=48000:channel_layouts=stereo",
            "-c:a", "pcm_s16le", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _make_video(path: Path, *, seconds: float = 15.0, fps: int = 24) -> None:
    subprocess.run(
        [
            FFMPEG, "-y", "-v", "error", "-f", "lavfi",
            "-i", f"testsrc=duration={seconds}:size=320x240:rate={fps}",
            "-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-q:v", "4", str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _upload(client: TestClient, project_id: str, path: Path, mime: str) -> str:
    response = client.post(
        f"/studio/api/projects/{project_id}/uploads",
        files={"file": (path.name, path.read_bytes(), mime)},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _plan_payload(
    source_id: str, sample_ids: dict[str, str], *, fps: int = 24, duration_s: float = 15.0
) -> dict:
    return {
        "source_video_asset_id": source_id,
        "fps": fps,
        "duration_s": duration_s,
        "onset_tolerance_s": 0.25,
        "actions": [
            {"action_id": "walk-a", "kind": "steps", "start_s": 1.0, "end_s": 6.0},
            {"action_id": "brake-a", "kind": "braking", "start_s": 7.0, "end_s": 9.0},
            {"action_id": "room-a", "kind": "ambience", "start_s": 0.0, "end_s": 15.0},
        ],
        "cues": [
            {
                "cue_id": "amb-on-1",
                "action_id": "room-a",
                "stem": "ambience",
                "sample_asset_id": sample_ids["amb"],
                "onset_s": 0.5,
                "loudness_lufs": -23.0,
                "sample_license": "user-owned",
                "sample_attribution": "Provided by project owner",
            },
            {
                "cue_id": "step-on-1",
                "action_id": "walk-a",
                "stem": "steps",
                "sample_asset_id": sample_ids["step"],
                "onset_s": 2.0,
                "loudness_lufs": -23.0,
                "sample_license": "user-owned",
                "sample_attribution": "Provided by project owner",
            },
            {
                "cue_id": "brake-on-1",
                "action_id": "brake-a",
                "stem": "braking",
                "sample_asset_id": sample_ids["brake"],
                "onset_s": 7.5,
                "loudness_lufs": -23.0,
                "sample_license": "user-owned",
                "sample_attribution": "Provided by project owner",
            },
        ],
    }


def test_scene_plan_persists_validated_plan(client: TestClient, tmp_path: Path) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Escena C25"}
    ).json()["id"]
    fake_video = tmp_path / "fake.mp4"
    fake_video.write_bytes(b"no es video real, solo registro de asset")
    fake_wav = tmp_path / "fake.wav"
    fake_wav.write_bytes(b"no es audio real")
    source_id = _upload(client, project_id, fake_video, "video/mp4")
    step_id = _upload(client, project_id, fake_wav, "audio/wav")
    plan_url = f"/studio/api/projects/{project_id}/sound/scene-plan"

    # Plan inválido: sin licencia declarada no se persiste.
    invalid = _plan_payload(source_id, {"step": step_id, "brake": step_id, "amb": step_id})
    invalid["cues"][1].pop("sample_license")
    assert client.put(plan_url, json=invalid).status_code == 422
    assert client.get(plan_url).status_code == 404

    # Plan válido se persiste y relee.
    valid = _plan_payload(source_id, {"step": step_id, "brake": step_id, "amb": step_id})
    assert client.put(plan_url, json=valid).status_code == 200
    stored = client.get(plan_url).json()
    assert stored["schema"] == "scene-sound-plan/1"
    assert stored["source_video_asset_id"] == source_id
    assert [cue["cue_id"] for cue in stored["cues"]] == ["amb-on-1", "step-on-1", "brake-on-1"]
    assert stored["audio_generation"] == "not_run"


def test_scene_plan_render_measures_and_persists_provenance(
    scene_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = scene_client
    project_id = client.post(
        "/studio/api/projects", json={"name": "Escena C25 render"}
    ).json()["id"]
    video = tmp_path / "walk15.mp4"
    _make_video(video)
    step = tmp_path / "step.wav"
    brake = tmp_path / "brake.wav"
    amb = tmp_path / "amb.wav"
    _make_sample(step, freq=440)
    _make_sample(brake, freq=880, seconds=1.0)
    _make_sample(amb, freq=220, seconds=15.0)
    source_id = _upload(client, project_id, video, "video/mp4")
    sample_ids = {
        "step": _upload(client, project_id, step, "audio/wav"),
        "brake": _upload(client, project_id, brake, "audio/wav"),
        "amb": _upload(client, project_id, amb, "audio/wav"),
    }
    plan_url = f"/studio/api/projects/{project_id}/sound/scene-plan"
    assert client.put(plan_url, json=_plan_payload(source_id, sample_ids)).status_code == 200

    response = client.post(f"{plan_url}/render")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["audio_generation"] == "ffmpeg_assembled"
    assert data["engine"] == "ffmpeg"
    stems = {entry["stem"]: entry for entry in data["stems"]}
    assert set(stems) == {"steps", "braking", "ambience"}
    assert abs(stems["steps"]["first_onset_s"] - 2.0) <= 0.01
    assert abs(stems["braking"]["first_onset_s"] - 7.5) <= 0.01
    assert abs(stems["ambience"]["first_onset_s"] - 0.5) <= 0.01
    assert stems["steps"]["file"] == "scene-stem-steps.wav"
    for entry in stems.values():
        assert entry["integrated_loudness_lufs"] <= -0.1
        assert entry["true_peak_dbfs"] < -0.1
    events = {event["cue_id"]: event for event in data["events"]}
    for cue_id, expected in (("step-on-1", 2.0), ("brake-on-1", 7.5), ("amb-on-1", 0.5)):
        assert abs(events[cue_id]["measured_onset_s"] - expected) <= 0.01
        assert events[cue_id]["verified"] is True

    provenance = client.get(f"{plan_url}/provenance").json()
    assert provenance["schema"] == "scene-sound-provenance/1"
    assert provenance["source_video"]["asset_id"] == source_id
    assert provenance["timebase"]["fps"] == 24
    assert provenance["stale"] is False
    assert all(entry.get("asset_id") for entry in provenance["stems"])
    assert {entry["file"] for entry in provenance["stems"]} == {
        "scene-stem-ambience.wav",
        "scene-stem-braking.wav",
        "scene-stem-steps.wav",
    }
    assert {sample["license"] for sample in provenance["samples"].values()} == {"user-owned"}
    stem_hashes = {entry["stem"]: entry["sha256"] for entry in provenance["stems"]}
    assert len(set(stem_hashes.values())) == 3  # stems separados y editables

    # Los stems persistidos son assets de salida del proyecto.
    assets = client.get(f"/studio/api/projects/{project_id}/assets?category=outputs").json()
    names = {asset.get("metadata", {}).get("artifact_kind") for asset in assets["assets"]}
    assert "scene_sound_stem" in names
    assert "scene_sound_provenance" in names

    changed_plan = _plan_payload(source_id, sample_ids)
    changed_plan["cues"][1]["onset_s"] = 2.25
    assert client.put(plan_url, json=changed_plan).status_code == 200
    stale_provenance = client.get(f"{plan_url}/provenance").json()
    assert stale_provenance["stale"] is True
    assert stale_provenance["current_plan_sha256"] != stale_provenance["plan_sha256"]

    def fail_render(*args, **kwargs):
        raise AudioProcessError("ffmpeg: timeout de 1s")

    monkeypatch.setattr("comfy_studio.routers.audio.render_scene_sound", fail_render)
    unavailable = client.post(f"{plan_url}/render")
    assert unavailable.status_code == 503
    assert "timeout" in unavailable.json()["detail"]


def test_scene_plan_requires_project_and_plan(scene_client: TestClient) -> None:
    client = scene_client
    missing_project = client.get("/studio/api/projects/nope/sound/scene-plan")
    assert missing_project.status_code == 404
    project_id = client.post(
        "/studio/api/projects", json={"name": "Escena vacía"}
    ).json()["id"]
    plan_url = f"/studio/api/projects/{project_id}/sound/scene-plan"
    assert client.get(plan_url).status_code == 404
    assert client.post(f"{plan_url}/render").status_code == 404
    assert client.get(f"{plan_url}/provenance").status_code == 404
