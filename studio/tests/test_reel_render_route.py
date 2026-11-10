from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from comfy_studio.engines import EngineResult, EngineRunSpec
from comfy_studio.engines.editor import EditorEngine


def test_reel_render_resolves_project_assets_and_persists_output(
    client: TestClient,
    monkeypatch,
) -> None:
    project = client.post("/studio/api/projects", json={"name": "reel"}).json()
    project_id = project["id"]
    uploaded = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    assert uploaded.status_code == 201
    asset_id = uploaded.json()["id"]

    def fake_probe(_path: Path, *, timeout_s: float) -> dict:
        assert timeout_s > 0
        return {
            "probe_ok": True,
            "has_video": True,
            "duration_sec": 4.0,
            "width": 320,
            "height": 240,
        }

    def fake_render(_engine: EditorEngine, spec: EngineRunSpec) -> EngineResult:
        assert spec.job["render_concurrency"] == 1
        assert spec.job["offthread_cache_bytes"] == 134_217_728
        assert spec.job["memory_efficient"] is True
        output = Path(spec.job["output_path"])
        output.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return EngineResult(
            ok=True,
            engine="editor",
            verb="run",
            payload={"output": str(output)},
        )

    monkeypatch.setattr("comfy_studio.perception.keyframes.probe_media_info", fake_probe)
    monkeypatch.setattr(EditorEngine, "run", fake_render)
    response = client.post(
        f"/studio/api/projects/{project_id}/reels/render",
        json={
            "clips": [{"asset_id": asset_id, "transition_in": "fade"}],
            "titles": [{"text": "Hola", "start_s": 0, "end_s": 2, "position": "top"}],
            "captions": [{"text": "Equipo", "start_s": 2, "end_s": 4}],
            "width": 320,
            "height": 240,
            "fps": 15,
            "preset_id": "custom-preview-v1",
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["status"] == "rendered"
    assert data["simulated"] is False
    output_id = data["output_asset"]["id"]
    output = client.get(f"/studio/api/projects/{project_id}/assets/{output_id}")
    assert output.status_code == 200
    assert output.json()["category"] == "outputs"
    assert output.json()["metadata"]["engine"] == "remotion"
    assert output.json()["metadata"]["preset_id"] == "custom-preview-v1"


def test_reel_preset_catalog_is_versioned_and_exposes_resource_limits(
    client: TestClient,
) -> None:
    response = client.get("/studio/api/reels/presets")
    assert response.status_code == 200
    presets = response.json()["presets"]
    vertical = next(item for item in presets if item["preset_id"] == "social-vertical-v1")
    assert vertical["max_duration_s"] == 60
    assert vertical["concurrency"] == 2
    assert vertical["offthread_cache_bytes"] == 268_435_456


def test_reel_render_rejects_duration_over_preset_ceiling(
    client: TestClient,
    monkeypatch,
) -> None:
    project = client.post("/studio/api/projects", json={"name": "too long"}).json()
    uploaded = client.post(
        f"/studio/api/projects/{project['id']}/assets",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    asset_id = uploaded.json()["id"]
    monkeypatch.setattr(
        "comfy_studio.perception.keyframes.probe_media_info",
        lambda *_args, **_kwargs: {
            "probe_ok": True,
            "has_video": True,
            "duration_sec": 61.0,
            "width": 320,
            "height": 240,
        },
    )
    response = client.post(
        f"/studio/api/projects/{project['id']}/reels/render",
        json={"clips": [{"asset_id": asset_id}]},
    )
    assert response.status_code == 422
    assert "60 s" in response.json()["detail"]


def test_reel_render_rejects_asset_from_another_project(client: TestClient) -> None:
    project_a = client.post("/studio/api/projects", json={"name": "a"}).json()
    project_b = client.post("/studio/api/projects", json={"name": "b"}).json()
    uploaded = client.post(
        f"/studio/api/projects/{project_a['id']}/assets",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    response = client.post(
        f"/studio/api/projects/{project_b['id']}/reels/render",
        json={"clips": [{"asset_id": uploaded.json()["id"]}]},
    )
    assert response.status_code == 400


def test_programmed_render_consumes_persisted_edl_time_ranges_and_overlays(
    client: TestClient,
    monkeypatch,
) -> None:
    project = client.post("/studio/api/projects", json={"name": "programmed EDL"}).json()
    project_id = project["id"]
    uploaded = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("clip.mp4", b"\x00\x00\x00\x18ftypmp42", "video/mp4")},
    )
    asset_id = uploaded.json()["id"]
    edl = client.post(
        f"/studio/api/projects/{project_id}/edl",
        json={
            "title": "Entrada editada",
            "aspect_ratio": "9:16",
            "source_asset_id": asset_id,
            "cuts": [
                {
                    "asset_id": asset_id,
                    "in_sec": 1.0,
                    "out_sec": 3.0,
                    "transition_in": "crossfade",
                    "transition_duration_s": 0.25,
                }
            ],
            "titles": [{"text": "Equipo", "start_s": 0, "end_s": 1.5}],
            "captions": [{"text": "Listo", "start_s": 0.5, "end_s": 2.0}],
        },
    )
    assert edl.status_code == 201, edl.text
    version = edl.json()["version"]

    def fake_probe(_path: Path, *, timeout_s: float) -> dict:
        return {
            "probe_ok": True,
            "has_video": True,
            "duration_sec": 2.0 if "studio-edl-remotion" in str(_path) else 4.0,
            "width": 320,
            "height": 240,
        }

    def fake_render(_engine: EditorEngine, spec: EngineRunSpec) -> EngineResult:
        clip = spec.job["clips"][0]
        assert clip["start_s"] == 1.0
        assert clip["duration_s"] == 2.0
        assert clip["transition_in"] == "crossfade"
        assert spec.job["titles"][0]["text"] == "Equipo"
        assert spec.job["captions"][0]["text"] == "Listo"
        output = Path(spec.job["output_path"])
        output.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        return EngineResult(ok=True, engine="editor", verb="run", payload={"output": str(output)})

    monkeypatch.setattr("comfy_studio.perception.keyframes.probe_media_info", fake_probe)
    monkeypatch.setattr(EditorEngine, "run", fake_render)
    response = client.post(
        f"/studio/api/projects/{project_id}/edl/{version}/programmed-render",
        json={"preset_id": "custom-preview-v1", "width": 320, "height": 240, "fps": 15},
    )
    assert response.status_code == 201, response.text
    assert response.json()["simulated"] is False
    assert response.json()["output_asset"]["metadata"]["edl_version"] == version
