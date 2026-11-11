"""Persistencia de lexicones y pistas de voz por proyecto (W19/A21, CPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def test_voice_assets_persist_across_app_restart(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Voz persistente"}).json()["id"]
    prefix = f"/studio/api/projects/{project_id}/voice"
    lexicon_url = f"{prefix}/lexicons/es-CO-paisa"
    lexicon = {
        "approved_variant": True,
        "entries": {"qué más": "ke mas", "pues": "pwes"},
    }
    assert client.put(lexicon_url, json=lexicon).status_code == 200
    track_url = f"{prefix}/tracks/ana"
    track = {
        "utterances": [
            {"text": "Qué más", "start_s": 0.0, "end_s": 0.8},
            {"text": "pues", "start_s": 0.8, "end_s": 1.1},
        ]
    }
    assert client.put(track_url, json=track).status_code == 200
    result = client.post(
        f"{lexicon_url}/validate",
        json={"sample": ["qué más", "pues"], "min_coverage": 1.0},
    )
    assert result.status_code == 200
    assert result.json()["approved"] is True

    before_restart = client.get(prefix).json()
    assert before_restart["synthesis"] == "not_run"

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        after_restart = resumed.get(prefix)
        assert after_restart.status_code == 200
        assert after_restart.json() == before_restart
        assert resumed.get(track_url).json() == {
            "speaker": "ana",
            "utterances": [
                {"speaker": "ana", "text": "Qué más", "start_s": 0.0, "end_s": 0.8},
                {"speaker": "ana", "text": "pues", "start_s": 0.8, "end_s": 1.1},
            ],
        }


def test_unapproved_or_insufficient_variant_is_not_certified(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Acento"}).json()["id"]
    url = f"/studio/api/projects/{project_id}/voice/lexicons/es-CO-paisa"
    assert client.put(url, json={"entries": {"pues": "pwes"}}).status_code == 200
    response = client.post(
        f"{url}/validate",
        json={"sample": ["pues"], "min_coverage": 0.8},
    )
    assert response.status_code == 200
    assert response.json()["approved"] is False
    assert "NO variante aprobada" in response.json()["reason"]

    assert (
        client.put(
            url,
            json={"approved_variant": True, "entries": {"pues": "pwes"}},
        ).status_code
        == 200
    )
    insufficient = client.post(
        f"{url}/validate",
        json={"sample": ["pues", "parce"], "min_coverage": 0.8},
    )
    assert insufficient.json()["approved"] is False
    assert insufficient.json()["coverage"] == 0.5


def test_overlapping_voice_lines_are_rejected_and_not_saved(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Voz inválida"}).json()["id"]
    prefix = f"/studio/api/projects/{project_id}/voice"
    response = client.put(
        f"{prefix}/tracks/beto",
        json={
            "utterances": [
                {"text": "uno", "start_s": 0.0, "end_s": 2.0},
                {"text": "dos", "start_s": 1.0, "end_s": 3.0},
            ]
        },
    )
    assert response.status_code == 422
    assert client.get(prefix).json()["tracks"] == {}
