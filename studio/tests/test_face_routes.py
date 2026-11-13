"""Rutas de curva facial por hablante persistidas por proyecto (W14/A16, CPU).

Curva de morph ligada a una pista de audio: guardado validado, idempotencia,
export/import con SHA-256 canónico (consistente con routers/motion.py) y
propiedad por proyecto. Sin generación neural: la procedencia lo declara.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

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

    conflict = client.put(_curve_url(project_id), json={**CURVE, "times": [0.0, 1.0, 3.0]})
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


def test_speaker_tracks_hash_and_preserve_project_audio_provenance(client: TestClient) -> None:
    project_id = _project(client, "Pistas de actores")
    other_project = _project(client, "Audio ajeno")
    store = client.app.state.store
    audio_a = store.save_upload(
        project_id,
        filename="ana.wav",
        content_type="audio/wav",
        data=b"fixture-audio-ana",
        max_upload_bytes=1024,
    )
    audio_b = store.save_upload(
        project_id,
        filename="luis.wav",
        content_type="audio/wav",
        data=b"fixture-audio-luis",
        max_upload_bytes=1024,
    )
    foreign = store.save_upload(
        other_project,
        filename="foreign.wav",
        content_type="audio/wav",
        data=b"foreign",
        max_upload_bytes=1024,
    )
    url = f"/studio/api/projects/{project_id}/face/speaker-tracks"
    turns = [
        {
            "speaker_id": "ana",
            "audio_track_id": "stem_ana",
            "audio_asset_id": audio_a["id"],
            "text": "Hola Medellín",
            "start": 0,
            "end": 1,
        },
        {
            "speaker_id": "luis",
            "audio_track_id": "stem_luis",
            "audio_asset_id": audio_b["id"],
            "text": "Qué tal",
            "start": 1,
            "end": 2,
        },
    ]
    response = client.put(url, json={"turns": turns})
    assert response.status_code == 200, response.text
    tracks = response.json()["tracks"]
    assert list(tracks) == ["ana", "luis"]
    assert tracks["ana"][0]["audio_sha256"] == hashlib.sha256(b"fixture-audio-ana").hexdigest()
    compiled = client.post(
        f"/studio/api/projects/{project_id}/face/speakers/ana/curves/hola/compile-spanish",
        json={
            "audio_track_id": "stem_ana",
            "rig_mapping": {
                "closed": "mouth_closed",
                "round": "mouth_round",
                "open": "jaw_open",
                "wide": "mouth_wide",
                "teeth": "mouth_teeth",
            },
        },
    )
    assert compiled.status_code == 200, compiled.text
    assert compiled.json()["provenance"]["audio_asset_id"] == audio_a["id"]
    assert compiled.json()["provenance"]["lip_quality"] == "fixture_only"
    rejected = client.put(url, json={"turns": [{**turns[0], "audio_asset_id": foreign["id"]}]})
    assert rejected.status_code == 422


def test_render_route_saves_real_blender_glb_to_project(client: TestClient) -> None:
    blender = shutil.which("blender")
    if not blender:
        import pytest

        pytest.skip("Blender not installed")
    project_id = _project(client, "Render de hablante")
    with tempfile.TemporaryDirectory() as tmp:
        actor_path = Path(tmp) / "actor.glb"
        expr = (
            "import bpy; bpy.ops.mesh.primitive_cube_add(); o=bpy.context.object; "
            "o.shape_key_add(name='Basis'); o.shape_key_add(name='jaw_open'); "
            f"bpy.ops.export_scene.gltf(filepath={str(actor_path)!r},"
            "export_format='GLB',export_morph=True)"
        )
        subprocess.run(
            [blender, "--background", "--python-expr", expr],
            check=True,
            capture_output=True,
            timeout=120,
        )
        source = client.app.state.store.save_upload(
            project_id,
            filename="actor.glb",
            content_type="model/gltf-binary",
            data=actor_path.read_bytes(),
            max_upload_bytes=1024 * 1024,
        )
    curve_url = _curve_url(project_id, "hola")
    assert (
        client.put(
            curve_url,
            json={**CURVE, "weights": [{"jaw_open": 0.0}, {"jaw_open": 0.8}, {"jaw_open": 0.1}]},
        ).status_code
        == 200
    )
    response = client.post(
        f"/studio/api/projects/{project_id}/face/actors/{source['id']}/curves/hola/render"
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["provenance"]["cpu_artifact"] is True
    assert result["provenance"]["neural_tts"] == "not_run"
    saved_path = client.app.state.store.get_asset_path(project_id, result["asset"]["id"])
    assert saved_path and saved_path.read_bytes()[:4] == b"glTF"
