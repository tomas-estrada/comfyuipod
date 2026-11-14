"""Persistencia e invalidación selectiva de pases AOV (W16/A18, CPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def test_aovs_persist_and_relight_invalidation_preserves_unrelated_passes(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "AOV persistente"}).json()["id"]
    url = f"/studio/api/projects/{project_id}/aovs"
    stored = {
        "aovs": {
            name: {"validity": "valid", "content_hash": f"hash-{name}"}
            for name in ("albedo", "normal", "depth", "roughness", "id", "shading", "shadow")
        }
    }
    response = client.put(url, json=stored)
    assert response.status_code == 200, response.text
    assert response.json()["render_validation"] == "not_run"

    relight = client.post(f"{url}/invalidate", json={"task": "relight"})
    assert relight.status_code == 200, relight.text
    assert set(relight.json()["invalidated"]) == {"shading", "shadow"}
    assert relight.json()["aovs"]["normal"]["validity"] == "valid"
    assert relight.json()["aovs"]["shading"]["content_hash"] is None

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        after_restart = resumed.get(url)
        assert after_restart.status_code == 200
        assert after_restart.json() == {
            key: value
            for key, value in relight.json().items()
            if key not in {"task", "invalidated"}
        }
        restyle = resumed.post(f"{url}/invalidate", json={"task": "restyle"})
        assert restyle.status_code == 200
        assert set(restyle.json()["invalidated"]) == {
            "albedo",
            "roughness",
            "shading",
            "shadow",
        }
        assert restyle.json()["aovs"]["depth"]["validity"] == "valid"


def test_corrupt_persisted_aov_set_fails_closed(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "AOV corrupto"}).json()["id"]
    client.app.state.store.set_meta(
        project_id,
        "ultra_aov_set_v1",
        {"schema": "unknown"},
    )
    assert client.get(f"/studio/api/projects/{project_id}/aovs").status_code == 409


def test_relight_render_route_persists_deterministic_png_asset(
    client: TestClient,
) -> None:
    import base64
    import hashlib

    from comfy_studio.relight import render_relight

    project_id = client.post("/studio/api/projects", json={"name": "Relight render"}).json()["id"]
    width, height = 2, 1
    albedo = bytes((255, 0, 0, 255, 0, 255, 0, 255))
    shading = bytes((255, 128))
    expected = render_relight(albedo, shading, width, height)
    response = client.post(
        f"/studio/api/projects/{project_id}/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
            "intensity": 1.0,
            "ambient": 0.0,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    asset = body["asset"]
    assert asset["category"] == "outputs"
    assert asset["mime_type"] == "image/png"
    assert asset["size_bytes"] == len(expected["png"])
    evidence = body["evidence"]
    assert evidence["sha256"] == hashlib.sha256(expected["png"]).hexdigest()
    assert evidence["albedo_sha256"] == expected["albedo_sha256"]
    assert evidence["shading_sha256"] == expected["shading_sha256"]

    # Longitud de buffer inconsistente → 422 (falla cerrada).
    invalid = client.post(
        f"/studio/api/projects/{project_id}/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo[:-1]).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
        },
    )
    assert invalid.status_code == 422
    # Proyecto inexistente → 404 antes de tocar píxeles.
    missing = client.post(
        f"/studio/api/projects/{project_id}-missing/aovs/relight/render",
        json={
            "albedo": base64.b64encode(albedo).decode("ascii"),
            "shading": base64.b64encode(shading).decode("ascii"),
            "width": width,
            "height": height,
        },
    )
    assert missing.status_code == 404


def test_temporal_relight_project_video_preserves_frame_timeline_and_reports_consistency(
    client: TestClient,
) -> None:
    """Project route applies a changing CPU light gain without retiming frames."""
    import json
    import shutil
    import subprocess

    import pytest

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe are required for the temporal video integration fixture")

    project_id = client.post("/studio/api/projects", json={"name": "Temporal relight"}).json()["id"]
    source_path = client.app.state.store.project_dir(project_id) / "uploads" / "source.mp4"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=32x32:r=2:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source_path),
        ],
        check=True,
        capture_output=True,
    )
    source_asset = client.app.state.store.save_upload(
        project_id,
        filename="source.mp4",
        content_type="video/mp4",
        data=source_path.read_bytes(),
        max_upload_bytes=client.app.state.config.max_upload_bytes,
        category="uploads",
    )
    source_path.unlink()

    response = client.post(
        f"/studio/api/projects/{project_id}/assets/{source_asset['id']}/relight-temporal",
        json={
            "keyframes": [
                {"frame": 0, "gain": 0.5},
                {"frame": 3, "gain": 1.5},
            ]
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    report = body["relighting"]
    assert body["source_asset_id"] == source_asset["id"]
    assert body["asset"]["category"] == "outputs"
    assert (
        body["asset"]["metadata"]["temporal_relight_v1"]["source_asset_id"]
        == source_asset["id"]
    )
    assert report["renderer"] == "ffmpeg-cpu-temporal-rgb-gain-v1"
    assert report["timeline"]["source_frames"] == report["timeline"]["output_frames"] == 4
    assert report["timeline"]["source_has_audio"] is True
    assert report["timeline"]["output_has_audio"] is True
    assert report["temporal_consistency"]["frames_compared"] == 4
    assert report["temporal_consistency"]["mean_abs_gain_step"] == pytest.approx(1 / 3)
    assert report["temporal_consistency"]["max_abs_gain_step"] == pytest.approx(1 / 3)
    measured_gains = [
        item["observed_luma_ratio"] for item in report["temporal_consistency"]["per_frame_gain"]
    ]
    assert len(measured_gains) == 4
    assert all(a < b for a, b in zip(measured_gains, measured_gains[1:], strict=False))
    assert report["temporal_consistency"]["gain_tracking_luma_mae"] < 0.04
    assert report["timeline"]["timestamps_match"] is True

    output_path = client.app.state.store.get_asset_path(project_id, body["asset"]["id"])
    assert output_path is not None
    probe = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(output_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    streams = json.loads(probe.stdout)["streams"]
    assert {stream["codec_type"] for stream in streams} == {"video", "audio"}
