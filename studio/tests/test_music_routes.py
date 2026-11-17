"""Persistencia del arreglo musical por proyecto (C26), metadata-only."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app


def _make_project(client: TestClient, name: str) -> str:
    return client.post("/studio/api/projects", json={"name": name}).json()["id"]


def _put_mix(client: TestClient, project_id: str, stems: list[str]) -> None:
    response = client.put(
        f"/studio/api/projects/{project_id}/sound/mix",
        json={
            "fps": 24,
            "duration_s": 20.0,
            "stems": {stem: [{"event_id": f"{stem}-1", "onset_s": 0.0}] for stem in stems},
        },
    )
    assert response.status_code == 200


def _arrangement_payload(stems: list[str]) -> dict:
    return {
        "bpm": 120.0,
        "duration_s": 20.0,
        "sections": [
            {
                "section_id": "hit-1",
                "kind": "hit",
                "start_s": 0.0,
                "duration_s": 2.0,
                "stem": stems[0],
            },
            {
                "section_id": "loop-1",
                "kind": "loop",
                "start_s": 2.0,
                "duration_s": 6.0,
                "stem": stems[-1],
                "variant": "chorus",
            },
        ],
    }


def test_arrangement_persists_idempotent_and_restart_safe(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = _make_project(client, "Musica C26")
    _put_mix(client, project_id, ["drums", "bass"])
    url = f"/studio/api/projects/{project_id}/music/arrangement"
    payload = _arrangement_payload(["drums", "bass"])

    first = client.put(url, json=payload)
    assert first.status_code == 200
    body = first.json()
    assert body["audio_generation"] == "not_run"
    assert body["generation_claim"] == "metadata_only"
    assert {s["kind"] for s in body["sections"]} == {"hit", "loop"}
    assert body["sections"][1]["variant"] == "chorus"

    # Idempotencia: el mismo PUT devuelve exactamente lo mismo.
    assert client.put(url, json=payload).json() == body
    assert client.get(url).json() == body

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        assert resumed.get(url).json() == body


def test_arrangement_requires_stems_of_this_project_mix(client: TestClient) -> None:
    project_id = _make_project(client, "Sin mix")
    url = f"/studio/api/projects/{project_id}/music/arrangement"

    # Sin mix almacenado en el proyecto: los stems no existen todavía.
    response = client.put(url, json=_arrangement_payload(["drums", "bass"]))
    assert response.status_code == 400
    assert client.get(url).status_code == 404

    # Mix con otros stems: la sección referencia un stem que no está.
    _put_mix(client, project_id, ["strings"])
    response = client.put(url, json=_arrangement_payload(["drums", "bass"]))
    assert response.status_code == 400
    assert client.get(url).status_code == 404

    # Con los stems correctos, el arreglo se guarda.
    _put_mix(client, project_id, ["drums", "bass"])
    assert client.put(url, json=_arrangement_payload(["drums", "bass"])).status_code == 200


def test_arrangement_rejects_foreign_project_stems(client: TestClient) -> None:
    other_id = _make_project(client, "Otro proyecto")
    _put_mix(client, other_id, ["drums", "bass"])
    project_id = _make_project(client, "Propio sin mix")
    url = f"/studio/api/projects/{project_id}/music/arrangement"

    # La propiedad es por proyecto: el mix de OTRO proyecto no acredita stems.
    response = client.put(url, json=_arrangement_payload(["drums", "bass"]))
    assert response.status_code == 400
    assert client.get(url).status_code == 404


def test_arrangement_invalid_payload_not_persisted(client: TestClient) -> None:
    project_id = _make_project(client, "Arreglo inválido")
    _put_mix(client, project_id, ["drums", "bass"])
    url = f"/studio/api/projects/{project_id}/music/arrangement"

    bad_bpm = _arrangement_payload(["drums", "bass"])
    bad_bpm["bpm"] = 0.0
    assert client.put(url, json=bad_bpm).status_code == 422

    too_long = _arrangement_payload(["drums", "bass"])
    too_long["duration_s"] = 21.0
    assert client.put(url, json=too_long).status_code == 422

    bad_kind = _arrangement_payload(["drums", "bass"])
    bad_kind["sections"][0]["kind"] = "solo"
    assert client.put(url, json=bad_kind).status_code == 422

    assert client.get(url).status_code == 404


def test_delete_arrangement(client: TestClient) -> None:
    project_id = _make_project(client, "Arreglo borrable")
    _put_mix(client, project_id, ["drums", "bass"])
    url = f"/studio/api/projects/{project_id}/music/arrangement"
    assert client.put(url, json=_arrangement_payload(["drums", "bass"])).status_code == 200

    deleted = client.delete(url)
    assert deleted.status_code == 200
    assert deleted.json()["removed_section_ids"] == ["hit-1", "loop-1"]
    assert client.get(url).status_code == 404
    # Sin arreglo previo: 404, no 500.
    assert client.delete(url).status_code == 404
