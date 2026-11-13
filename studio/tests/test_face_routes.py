"""Rutas de curva facial por hablante persistidas por proyecto (W14/A16, CPU).

Curva de morph ligada a una pista de audio: guardado validado, idempotencia,
export/import con SHA-256 canónico (consistente con routers/motion.py) y
propiedad por proyecto. Sin generación neural: la procedencia lo declara.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from comfy_studio.app import create_app

CURVE = {
    "speaker_id": "actor_ana",
    "audio_track_id": "voz_ana",
    "times": [0.0, 1.0, 2.0],
    "weights": [
        {"jaw_open": 0.0, "smile": 0.1},
        {"jaw_open": 0.8, "smile": 0.2},
        {"jaw_open": 0.2, "smile": 0.0},
    ],
}


def _project(client: TestClient, name: str) -> str:
    return client.post("/studio/api/projects", json={"name": name}).json()["id"]


def _curve_url(project_id: str, curve_id: str = "curva_ana") -> str:
    return f"/studio/api/projects/{project_id}/face/curves/{curve_id}"


def _assert_no_face_meta_written(client: TestClient, project_id: str) -> None:
    curves = client.get(f"/studio/api/projects/{project_id}/face/curves").json()
    assert curves == {"curves": {}}


def _save_fixture(client: TestClient, project_id: str) -> None:
    response = client.put(_curve_url(project_id), json=CURVE)
    assert response.status_code == 200, response.text


def test_curve_save_validate_and_persist(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = _project(client, "Curva facial")
    response = client.put(_curve_url(project_id), json=CURVE)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["speaker_id"] == "actor_ana"
    assert saved["audio_track_id"] == "voz_ana"
    assert saved["provenance"]["neural_generation"] == "not_used"

    before_restart = client.get(_curve_url(project_id)).json()
    assert before_restart["curve"]["times"] == [0.0, 1.0, 2.0]

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        assert resumed.get(_curve_url(project_id)).json() == before_restart


def test_invalid_curves_fail_closed(client: TestClient) -> None:
    project_id = _project(client, "Curva inválida")

    cases: list[dict] = [
        {**CURVE, "times": [-0.5, 1.0, 2.0]},  # tiempo negativo
        {**CURVE, "times": [0.0, 1.0, 1.0]},  # claves duplicadas
        {**CURVE, "times": [0.0, 1.0]},  # longitudes desiguales
        {**CURVE, "times": [], "weights": []},  # curva vacía
        {
            **CURVE,
            "weights": [
                {"jaw_open": 1.5},
                {"jaw_open": 0.0},
                {"jaw_open": 0.0},
            ],
        },
    ]
    for payload in cases:
        response = client.put(_curve_url(project_id), json=payload)
        assert response.status_code == 409, f"{payload!r}: {response.text}"
    _assert_no_face_meta_written(client, project_id)

    # inf no es JSON válido: httpx rechaza el literal antes de enrutar, así que
    # el caso no finito se envía como bytes crudos ("Infinity" de json.dumps;
    # el servidor lo parsea y la validación lo rechaza, falla cerrada).
    nonfinite = {
        **CURVE,
        "times": [0.0, 1.0, 2.0],
        "weights": [
            {"jaw_open": 0.0},
            {"jaw_open": float("inf")},
            {"jaw_open": 0.0},
        ],
    }
    response = client.put(
        _curve_url(project_id),
        content=json.dumps(nonfinite).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 409, response.text
    _assert_no_face_meta_written(client, project_id)


def test_rewrites_are_idempotent_but_conflicts_fail(client: TestClient) -> None:
    project_id = _project(client, "Reintentos de curva")
    _save_fixture(client, project_id)
    assert client.put(_curve_url(project_id), json=CURVE).status_code == 200

    conflict = client.put(
        _curve_url(project_id), json={**CURVE, "times": [0.0, 1.0, 3.0]}
    )
    assert conflict.status_code == 409
    assert client.get(_curve_url(project_id)).json()["curve"]["times"] == [0.0, 1.0, 2.0]


def test_project_ownership_fails_closed(client: TestClient) -> None:
    project_id = _project(client, "Propiedad de curva")
    _save_fixture(client, project_id)
    other = _project(client, "Otro proyecto")

    assert client.get("/studio/api/projects/no-existe/face/curves").status_code == 404
    assert client.get("/studio/api/projects/no-existe/face/curves/x").status_code == 404
    assert client.get(_curve_url(other)).status_code == 404
    assert client.get(f"/studio/api/projects/{other}/face/curves").json() == {"curves": {}}


def test_export_import_round_trip_is_reexecutable(client: TestClient) -> None:
    project_a = _project(client, "Origen curva")
    project_b = _project(client, "Destino curva")
    _save_fixture(client, project_a)

    export_url = f"{_curve_url(project_a)}/export"
    first = client.get(export_url).json()
    second = client.get(export_url).json()
    assert first == second  # determinista
    assert first["schema"] == "ultra-face-curve-export-v1"
    assert first["origin"]["project_id"] == project_a
    assert first["curve"]["audio_track_id"] == "voz_ana"
    assert len(first["content_hash"]) == 64  # sha256 hex

    imported = client.post(f"/studio/api/projects/{project_b}/face/import", json=first)
    assert imported.status_code == 200, imported.text
    assert client.get(_curve_url(project_b)).json()["curve"] == first["curve"]

    tampered = {**first, "curve": {**first["curve"], "times": [0.0, 1.0, 3.0]}}
    rejected = client.post(f"/studio/api/projects/{project_b}/face/import", json=tampered)
    assert rejected.status_code == 409
    # la importación válida previa sobrevive intacta (falla cerrada sin borrar)
    assert client.get(_curve_url(project_b)).json()["curve"] == first["curve"]

    wrong_schema = {**first, "schema": "otro-schema"}
    rejected_schema = client.post(
        f"/studio/api/projects/{project_b}/face/import", json=wrong_schema
    )
    assert rejected_schema.status_code == 409


def test_import_rejects_malformed_json_bytes(client: TestClient) -> None:
    project_id = _project(client, "Importación JSON malformado")
    response = client.post(
        f"/studio/api/projects/{project_id}/face/import",
        content=b'{"schema": "ultra-face-curve-export-v1", "curve":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 409, response.text
    _assert_no_face_meta_written(client, project_id)
