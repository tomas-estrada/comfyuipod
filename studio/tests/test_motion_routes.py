"""Rutas de movimiento persistidas por proyecto (C11-C14 / A15, CPU).

Esqueleto/clip guardados, validación, retarget determinista y export/import
reejecutable sobre el ProjectStore. Sin generación neural ni captura: la
procedencia lo declara y los motores ausentes (Isaac Gym/ReMoGen) no se finges.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.app import create_app

SKELETON_A = {
    "name": "rig_a",
    "root": "root",
    "scale": 1.0,
    "bones": [
        {"name": "root", "parent": None},
        {"name": "spine", "parent": "root", "min_deg": -45.0, "max_deg": 45.0},
        {"name": "arm_L", "parent": "spine", "min_deg": 0.0, "max_deg": 160.0},
    ],
}

SKELETON_B = {
    "name": "rig_b",
    "root": "root",
    "scale": 2.0,
    "bones": [
        {"name": "root", "parent": None},
        {"name": "spine", "parent": "root", "min_deg": -30.0, "max_deg": 30.0},
        {"name": "arm_L", "parent": "spine", "min_deg": -10.0, "max_deg": 120.0},
    ],
}

CLIP = {
    "name": "walk",
    "fps": 24,
    "frames": [{"spine": 10.0, "arm_L": 30.0}, {"spine": 20.0, "arm_L": 200.0}],
    "root_track": [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0]],
    "contacts": {"foot_L": [0]},
    "contact_events": [
        {
            "effector": "foot_L",
            "frame": 0,
            "position": [0.0, -1.0, 0.2],
            "surface": "floor",
        }
    ],
    "identity_id": "actor-7",
    "action_id": "walk-cycle",
    "mask_asset_id": "mask-1",
    "camera_context": {"shot": "A"},
}

MAPPING = {"spine": "spine", "arm_L": "arm_L"}


def _project(client: TestClient, name: str) -> str:
    return client.post("/studio/api/projects", json={"name": name}).json()["id"]


def _skeleton_url(project_id: str, skeleton_id: str = "rig_a") -> str:
    return f"/studio/api/projects/{project_id}/motion/skeletons/{skeleton_id}"


def _clip_url(project_id: str, clip_id: str = "walk") -> str:
    return f"/studio/api/projects/{project_id}/motion/clips/{clip_id}"


def _import_url(project_id: str) -> str:
    return f"/studio/api/projects/{project_id}/motion/import"


def _assert_no_motion_meta_written(client: TestClient, project_id: str) -> None:
    skeletons = client.get(
        f"/studio/api/projects/{project_id}/motion/skeletons"
    ).json()
    clips = client.get(f"/studio/api/projects/{project_id}/motion/clips").json()
    assert skeletons == {"skeletons": {}}
    assert clips == {"clips": {}}


def _save_fixture(client: TestClient, project_id: str) -> None:
    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    assert (
        client.put(
            _skeleton_url(project_id, "rig_b"), json=SKELETON_B
        ).status_code
        == 200
    )
    response = client.put(
        _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP}
    )
    assert response.status_code == 200, response.text


def test_skeleton_and_clip_save_validate_and_persist(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = _project(client, "Movimiento")
    response = client.put(_skeleton_url(project_id), json=SKELETON_A)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["name"] == "rig_a"
    assert saved["gpu_validation"] == "not_run"

    clip_response = client.put(
        _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP}
    )
    assert clip_response.status_code == 200, clip_response.text

    before_restart = client.get(_clip_url(project_id)).json()
    assert before_restart["clip"]["fps"] == 24
    assert before_restart["clip"]["identity_id"] == "actor-7"
    assert before_restart["clip"]["action_id"] == "walk-cycle"
    assert before_restart["clip"]["mask_asset_id"] == "mask-1"
    assert before_restart["clip"]["camera_context"] == {"shot": "A"}
    assert before_restart["clip"]["contacts"] == {"foot_L": [0]}
    assert before_restart["skeleton_id"] == "rig_a"

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        assert resumed.get(_clip_url(project_id)).json() == before_restart
        assert resumed.get(_skeleton_url(project_id)).json()["name"] == "rig_a"


def test_invalid_skeleton_clip_and_references_fail_closed(client: TestClient) -> None:
    project_id = _project(client, "Movimiento inválido")

    duplicate = {**SKELETON_A, "bones": [*SKELETON_A["bones"], SKELETON_A["bones"][1]]}
    assert client.put(_skeleton_url(project_id), json=duplicate).status_code == 409

    bad_limits = {
        **SKELETON_A,
        "bones": [{"name": "root", "parent": None, "min_deg": 90.0, "max_deg": -90.0}],
    }
    assert client.put(_skeleton_url(project_id, "bad"), json=bad_limits).status_code == 409

    unknown_bone = {**CLIP, "frames": [{"hueso_fantasma": 1.0}]}
    response = client.put(
        _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": unknown_bone}
    )
    assert response.status_code == 409

    out_of_range_contact = {**CLIP, "contacts": {"foot_L": [99]}}
    response = client.put(
        _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": out_of_range_contact}
    )
    assert response.status_code == 409

    nonfinite_timebase = {**CLIP, "fps": 0}
    response = client.put(
        _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": nonfinite_timebase}
    )
    assert response.status_code == 409

    missing_skeleton = client.put(
        _clip_url(project_id), json={"skeleton_id": "no_existe", "clip": CLIP}
    )
    assert missing_skeleton.status_code == 409


def test_put_clip_maps_malformed_document_to_conflict(client: TestClient) -> None:
    project_id = _project(client, "Clip estructuralmente inválido")
    _save_fixture(client, project_id)
    before = client.get(f"/studio/api/projects/{project_id}/motion/clips").json()
    response = client.put(
        _clip_url(project_id),
        json={"skeleton_id": "rig_a", "clip": {"frames": []}},
    )
    assert response.status_code == 409, response.text
    assert client.get(f"/studio/api/projects/{project_id}/motion/clips").json() == before


def test_rewrites_are_idempotent_but_conflicts_fail(client: TestClient) -> None:
    project_id = _project(client, "Reintentos de movimiento")
    _save_fixture(client, project_id)

    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    conflict = client.put(
        _skeleton_url(project_id), json={**SKELETON_A, "scale": 3.0}
    )
    assert conflict.status_code == 409
    assert client.get(_skeleton_url(project_id)).json()["scale"] == 1.0

    assert (
        client.put(_clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP}).status_code
        == 200
    )
    clip_conflict = client.put(
        _clip_url(project_id),
        json={"skeleton_id": "rig_a", "clip": {**CLIP, "fps": 12}},
    )
    assert clip_conflict.status_code == 409
    assert client.get(_clip_url(project_id)).json()["clip"]["fps"] == 24


def test_retarget_persists_and_preserves_timebase_root_channels(
    client: TestClient,
) -> None:
    project_id = _project(client, "Retarget")
    _save_fixture(client, project_id)

    response = client.post(
        f"{_clip_url(project_id)}/retarget",
        json={
            "target_skeleton_id": "rig_b",
            "mapping": MAPPING,
            "clip_id": "walk_b",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    retargeted = body["clip"]
    assert retargeted["fps"] == 24
    assert len(retargeted["frames"]) == 2
    assert retargeted["frames"][1]["arm_L"] == 120.0  # límites del destino (clamp)
    assert retargeted["root_track"][1] == [0.2, 0.0, 0.0]  # raíz conservada ×2
    assert retargeted["identity_id"] == "actor-7"
    assert retargeted["action_id"] == "walk-cycle"
    assert retargeted["mask_asset_id"] == "mask-1"
    assert retargeted["camera_context"] == {"shot": "A"}
    assert retargeted["contact_events"][0]["position"] == [0.0, -2.0, 0.4]
    assert retargeted["contacts"] == {"foot_L": [0]}
    assert body["provenance"]["origin"] == "deterministic_retarget"
    assert body["provenance"]["neural_generation"] == "not_used"
    assert body["provenance"]["performance_capture"] == "not_performed"
    assert body["engines"]["isaac_gym"] == "unavailable"
    assert body["engines"]["remogen"] == "unavailable"
    assert body["gpu_validation"] == "not_run"

    persisted = client.get(_clip_url(project_id, "walk_b")).json()
    assert persisted["clip"] == retargeted
    assert persisted["skeleton_id"] == "rig_b"
    assert persisted["provenance"]["source_clip_id"] == "walk"
    assert persisted["provenance"]["mapping"] == MAPPING

    incomplete = client.post(
        f"{_clip_url(project_id)}/retarget",
        json={
            "target_skeleton_id": "rig_b",
            "mapping": {"spine": "spine"},
            "clip_id": "walk_partial",
        },
    )
    assert incomplete.status_code == 409


def test_engine_status_declares_missing_motion_engines(client: TestClient) -> None:
    project_id = _project(client, "Motores de movimiento")
    engines = client.get(f"/studio/api/projects/{project_id}/motion/engines").json()
    assert engines["isaac_gym"] == "unavailable"
    assert engines["remogen"] == "unavailable"
    assert engines["retarget_engine"] == "cpu-deterministic-rules"
    assert engines["neural_generation"] == "not_used"
    assert engines["gpu_validation"] == "not_run"


def test_export_import_round_trip_is_reexecutable(client: TestClient) -> None:
    project_a = _project(client, "Origen")
    project_b = _project(client, "Destino")
    _save_fixture(client, project_a)

    export_url = f"{_clip_url(project_a)}/export"
    first = client.get(export_url).json()
    second = client.get(export_url).json()
    assert first == second  # determinista
    assert first["schema"] == "ultra-motion-export-v1"
    assert first["skeleton"]["name"] == "rig_a"
    assert first["clip"]["fps"] == 24
    assert first["provenance"]["origin"] == "declared_keyframes"
    assert first["origin"]["project_id"] == project_a

    imported = client.post(f"/studio/api/projects/{project_b}/motion/import", json=first)
    assert imported.status_code == 200, imported.text

    # Unknown fields are discarded before the normalized payload hash is
    # checked/persisted, so export -> import remains repeatable.
    compatible = {
        **first,
        "skeleton": {**first["skeleton"], "extra": "ignorar"},
        "clip": {**first["clip"], "extra": "ignorar"},
    }
    normalized_import = client.post(
        f"/studio/api/projects/{project_b}/motion/import", json=compatible
    )
    assert normalized_import.status_code == 200, normalized_import.text
    assert "extra" not in normalized_import.json()["clip"]
    normalized_export = client.get(f"{_clip_url(project_b)}/export").json()
    assert "extra" not in normalized_export["skeleton"]
    reimport_project = _project(client, "Reimportación normalizada")
    reimported = client.post(
        f"/studio/api/projects/{reimport_project}/motion/import",
        json=normalized_export,
    )
    assert reimported.status_code == 200, reimported.text
    # el esqueleto destino es del proyecto destino (propiedad por proyecto).
    assert client.put(_skeleton_url(project_b, "rig_b"), json=SKELETON_B).status_code == 200

    for project_id in (project_a, project_b):
        retargeted = client.post(
            f"{_clip_url(project_id)}/retarget",
            json={
                "target_skeleton_id": "rig_b",
                "mapping": MAPPING,
                "clip_id": "walk_b",
            },
        )
        assert retargeted.status_code == 200, retargeted.text
    assert (
        client.get(_clip_url(project_a, "walk_b")).json()["clip"]
        == client.get(_clip_url(project_b, "walk_b")).json()["clip"]
    )

    tampered = {**first, "clip": {**first["clip"], "fps": 12}}
    rejected = client.post(f"/studio/api/projects/{project_b}/motion/import", json=tampered)
    assert rejected.status_code == 409


def test_import_rejects_malformed_json_bytes(client: TestClient) -> None:
    project_id = _project(client, "Importación JSON malformado")
    response = client.post(
        _import_url(project_id),
        content=b'{"schema": "ultra-motion-export-v1", "clip":',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 409, response.text
    _assert_no_motion_meta_written(client, project_id)


def test_import_rejects_null_and_list_bodies(client: TestClient) -> None:
    project_id = _project(client, "Importación JSON no objeto")
    for payload in (b"null", b"[1, 2]"):
        response = client.post(
            _import_url(project_id),
            content=payload,
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 409, f"{payload!r}: {response.text}"
        _assert_no_motion_meta_written(client, project_id)


def test_project_ownership_fails_closed(client: TestClient) -> None:
    project_id = _project(client, "Propiedad")
    _save_fixture(client, project_id)
    other = _project(client, "Otro proyecto")

    assert client.get("/studio/api/projects/no-existe/motion/clips").status_code == 404
    assert (
        client.post(
            "/studio/api/projects/no-existe/motion/import", json={"schema": "x"}
        ).status_code
        == 404
    )

    other_clips = client.get(
        f"/studio/api/projects/{other}/motion/clips"
    ).json()["clips"]
    assert other_clips == {}
    assert client.get(_clip_url(other)).status_code == 404

    imported = client.post(
        f"/studio/api/projects/{other}/motion/import",
        json=client.get(f"{_clip_url(project_id)}/export").json(),
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["provenance"]["source_project_id"] == project_id
