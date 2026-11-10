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
