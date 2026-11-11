"""Persistencia de eventos y stems causales por proyecto (W18/A20, CPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def test_project_mix_persists_and_causal_delete_is_selective(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Audio causal"}).json()["id"]
    mix_url = f"/studio/api/projects/{project_id}/sound/mix"
    payload = {
        "fps": 24,
        "duration_s": 3.0,
        "stems": {
            "foley": [
                {"event_id": "step-1", "onset_s": 0.5, "caused_by": "walk"},
                {"event_id": "door-1", "onset_s": 1.2, "caused_by": "door"},
            ],
            "ambience": [{"event_id": "wind", "onset_s": 0.0}],
        },
    }
    assert client.put(mix_url, json=payload).status_code == 200
    before_restart = client.get(mix_url).json()
    assert before_restart["event_count"] == 3
    assert before_restart["audio_generation"] == "not_run"

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        assert resumed.get(mix_url).json() == before_restart
        deleted = resumed.delete(f"/studio/api/projects/{project_id}/sound/causes/walk")
        assert deleted.status_code == 200
        assert deleted.json()["removed_event_ids"] == ["step-1"]
        events = {
            event["event_id"]
            for stem_events in deleted.json()["stems"].values()
            for event in stem_events
        }
        assert events == {"door-1", "wind"}


def test_invalid_audio_mix_is_not_persisted(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Audio inválido"}).json()["id"]
    mix_url = f"/studio/api/projects/{project_id}/sound/mix"
    invalid = {
        "fps": 24,
        "duration_s": 1.0,
        "stems": {
            "foley": [{"event_id": "same", "onset_s": 0.0}],
            "music": [{"event_id": "same", "onset_s": 0.5}],
        },
    }
    response = client.put(mix_url, json=invalid)
    assert response.status_code == 422
    assert client.get(mix_url).status_code == 404


def test_corrupt_persisted_mix_fails_closed_with_conflict(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Audio corrupto"}).json()["id"]
    client.app.state.store.set_meta(
        project_id,
        "ultra_audio_mix_v1",
        {"schema": "unknown-audio-schema"},
    )
    mix_url = f"/studio/api/projects/{project_id}/sound/mix"
    assert client.get(mix_url).status_code == 409
    assert client.delete(f"/studio/api/projects/{project_id}/sound/causes/walk").status_code == 409
