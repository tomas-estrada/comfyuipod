"""Integración API/SQLite de memoria espacial (W17/A19, CPU)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def test_spatial_memory_persists_and_resumes_after_app_restart(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Memoria espacial"}
    ).json()["id"]
    anchor_url = f"/studio/api/projects/{project_id}/spatial-memory/anchors/bridge"
    assert client.put(anchor_url, json={"provenance": "observed"}).status_code == 200
    visits_url = f"{anchor_url}/visits"
    for shot_id, position in (
        ("A", [10.0, 0.0, 5.0]),
        ("B", [20.0, 0.0, 5.0]),
        ("A2", [10.0, 0.0, 5.0]),
    ):
        response = client.post(
            visits_url,
            json={"shot_id": shot_id, "position": position},
        )
        assert response.status_code == 200, response.text

    before_restart = client.get(
        f"/studio/api/projects/{project_id}/spatial-memory"
    ).json()
    assert before_restart["drift"]["bridge"] == 0.0
    assert before_restart["gpu_validation"] == "not_run"

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        after_restart = resumed.get(
            f"/studio/api/projects/{project_id}/spatial-memory"
        ).json()
        assert after_restart == before_restart
        report = resumed.get(f"{anchor_url}/round-trip")
        assert report.status_code == 200, report.text
        assert report.json() == {
            "anchor_id": "bridge",
            "shots": ["A", "B", "A2"],
            "drift": 0.0,
            "returned": True,
        }


def test_spatial_memory_retries_are_idempotent_and_conflicts_fail(
    client: TestClient,
) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Reintento espacial"}
    ).json()["id"]
    anchor_url = f"/studio/api/projects/{project_id}/spatial-memory/anchors/anchor"
    assert client.put(anchor_url, json={"provenance": "inferred"}).status_code == 200
    visits_url = f"{anchor_url}/visits"
    visit = {"shot_id": "shot-1", "position": [1.0, 2.0, 3.0]}
    assert client.post(visits_url, json=visit).status_code == 200
    assert client.post(visits_url, json=visit).status_code == 200
    assert len(
        client.get(f"/studio/api/projects/{project_id}/spatial-memory").json()["visits"][
            "anchor"
        ]
    ) == 1

    conflict = client.post(
        visits_url,
        json={"shot_id": "shot-1", "position": [9.0, 2.0, 3.0]},
    )
    assert conflict.status_code == 409
