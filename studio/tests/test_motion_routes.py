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
    skeletons = client.get(f"/studio/api/projects/{project_id}/motion/skeletons").json()
    clips = client.get(f"/studio/api/projects/{project_id}/motion/clips").json()
    assert skeletons == {"skeletons": {}}
    assert clips == {"clips": {}}


def _save_fixture(client: TestClient, project_id: str) -> None:
    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    assert client.put(_skeleton_url(project_id, "rig_b"), json=SKELETON_B).status_code == 200
    response = client.put(_clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP})
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

    clip_response = client.put(_clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP})
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
    conflict = client.put(_skeleton_url(project_id), json={**SKELETON_A, "scale": 3.0})
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


def test_rig_build_creates_project_owned_blender_artifact(client: TestClient) -> None:
    import shutil

    if not shutil.which("blender"):
        return
    project_id = _project(client, "Rig editable")
    skeleton = {**SKELETON_A, "body_class": "humanoid", "identity_id": "actor-7"}
    assert client.put(_skeleton_url(project_id), json=skeleton).status_code == 200
    clip = {
        "name": "extreme",
        "fps": 12,
        "frames": [{"spine": 0.0}, {"spine": 35.0}],
        "root_track": [[0, 0, 0], [0, 0, 0]],
    }
    response = client.post(f"{_skeleton_url(project_id)}/build", json={"clip": clip})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["engine"] == "blender_cpu"
    assert body["rig"]["reimport_bones"] == 3
    assert body["rig"]["reimport_skinned_meshes"] == 1
    asset = body["asset"]
    assert asset["kind"] == "model3d"
    assert client.get(f"/studio/api/projects/{project_id}/assets/{asset['id']}").status_code == 200


def test_recovery_endpoint_persists_camera_compensated_track_and_contacts(
    client: TestClient,
) -> None:
    project_id = _project(client, "Recovery")
    skeleton = {**SKELETON_A, "identity_id": "actor-7"}
    assert client.put(_skeleton_url(project_id), json=skeleton).status_code == 200
    response = client.post(
        f"/studio/api/projects/{project_id}/motion/recover",
        json={
            "skeleton_id": "rig_a",
            "clip_id": "recovered",
            "name": "walk",
            "fps": 24,
            "frames": [
                {"root": [1, 0, 0], "foot_L": [1, 0, 0]},
                {"root": [2, 0, 0], "foot_L": [2.01, 0, 0]},
            ],
            "camera_track": [[0, 0, 0], [1, 0, 0]],
            "camera_yaw_radians": [0, 0],
            "subject_scale": 1,
        },
    )
    assert response.status_code == 200, response.text
    saved = client.get(_clip_url(project_id, "recovered")).json()
    assert saved["clip"]["root_track"] == [[1, 0, 0], [1, 0, 0]]
    assert saved["clip"]["contacts"] == {"foot_L": [0, 1]}
    assert saved["provenance"]["origin"] == "declared_3d_observations"


def test_choreography_and_acting_transfer_are_persisted_and_type_safe(client: TestClient) -> None:
    project_id = _project(client, "Choreography")
    _save_fixture(client, project_id)
    for clip_id, action in (("turn", "turn"), ("pose", "pose")):
        clip = {
            **CLIP,
            "name": action,
            "frames": [{"spine": 5.0}] * 2,
            "root_track": [[0, 0, 0]] * 2,
        }
        assert (
            client.put(
                _clip_url(project_id, clip_id), json={"skeleton_id": "rig_a", "clip": clip}
            ).status_code
            == 200
        )
    response = client.post(
        f"/studio/api/projects/{project_id}/motion/choreography",
        json={"clip_ids": ["walk", "turn", "pose"], "clip_id": "dance", "beat_frames": [0, 2, 4]},
    )
    assert response.status_code == 200, response.text
    dance = client.get(_clip_url(project_id, "dance")).json()["clip"]
    assert dance["action_id"] == "walk+turn+pose"
    assert dance["contacts"]["foot_L"] == [0, 2, 4]
    transfer = client.post(
        f"{_clip_url(project_id)}/transfer",
        json={"target_skeleton_id": "rig_b", "mapping": MAPPING, "clip_id": "walk_b"},
    )
    assert transfer.status_code == 200, transfer.text
    assert client.get(_clip_url(project_id, "walk_b")).json()["clip"]["mask_asset_id"] == "mask-1"
    vehicle = {**SKELETON_B, "body_class": "vehicle"}
    assert client.put(_skeleton_url(project_id, "vehicle"), json=vehicle).status_code == 200
    persisted_vehicle = client.get(_skeleton_url(project_id, "vehicle")).json()
    assert persisted_vehicle["body_class"] == "vehicle"
    rejected = client.post(
        f"{_clip_url(project_id)}/transfer",
        json={"target_skeleton_id": "vehicle", "mapping": MAPPING, "clip_id": "walk_car"},
    )
    assert rejected.status_code == 409


def test_motion_endpoints_reject_oversized_json_before_persisting(client: TestClient) -> None:
    project_id = _project(client, "Bounded motion")
    oversized = b'{"padding":"' + b"x" * (1024 * 1024) + b'"}'
    response = client.post(
        f"/studio/api/projects/{project_id}/motion/recover",
        content=oversized,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert client.get(f"/studio/api/projects/{project_id}/motion/clips").json() == {"clips": {}}


def test_all_new_motion_routes_bound_raw_request_bodies(client: TestClient) -> None:
    project_id = _project(client, "Every body bounded")
    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    assert (
        client.put(_clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP}).status_code
        == 200
    )
    oversized = b'{"padding":"' + b"x" * (1024 * 1024) + b'"}'
    cases = [
        ("POST", f"{_skeleton_url(project_id)}/build", oversized),
        ("POST", f"/studio/api/projects/{project_id}/motion/choreography", oversized),
        ("POST", f"{_clip_url(project_id)}/transfer", oversized),
    ]
    for method, url, body in cases:
        response = client.request(
            method, url, content=body, headers={"content-type": "application/json"}
        )
        assert response.status_code == 413, (url, response.text)


def test_oversized_blender_output_is_rejected_before_read_or_save(
    client: TestClient, tmp_path, monkeypatch
) -> None:
    from comfy_studio import blender_motion

    project_id = _project(client, "Large Blender output")
    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    output = tmp_path / "oversized.glb"
    with output.open("wb") as stream:
        stream.truncate(2 * 1024 * 1024 + 1)
    from pathlib import Path

    original_read_bytes = Path.read_bytes

    def forbid_oversized_read(path: Path) -> bytes:
        if path == output:
            raise AssertionError("oversized Blender output must not be read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", forbid_oversized_read)

    async def fake_blender_run(_spec):
        return blender_motion.BlenderRigResult(
            ok=True, glb_path=output, report={"reimport_bones": 3}
        )

    monkeypatch.setattr(blender_motion, "run_blender_rig", fake_blender_run)
    response = client.post(
        f"{_skeleton_url(project_id)}/build",
        json={"clip": {"name": "small", "fps": 24, "frames": [{"spine": 0.0}]}},
    )
    assert response.status_code == 409
    assert "upload limit" in response.json()["detail"]
    assert client.get(f"/studio/api/projects/{project_id}/assets").json()["assets"] == []


def test_motion_limits_reject_oversized_skeleton_clip_and_mapping(client: TestClient) -> None:
    project_id = _project(client, "Motion bounds")
    too_many_bones = {
        "name": "large",
        "root": "b0",
        "bones": [
            {"name": "b0", "parent": None},
            *[{"name": f"b{i}", "parent": "b0"} for i in range(1, 257)],
        ],
    }
    assert client.put(_skeleton_url(project_id), json=too_many_bones).status_code == 409
    assert client.put(_skeleton_url(project_id), json=SKELETON_A).status_code == 200
    recovery_response = client.post(
        f"/studio/api/projects/{project_id}/motion/recover",
        json={
            "skeleton_id": "rig_a",
            "clip_id": "too_long",
            "name": "long",
            "fps": 24,
            "frames": [{"root": [0, 0, 0]}] * 601,
            "camera_track": [[0, 0, 0]] * 601,
            "subject_scale": 1.0,
        },
    )
    assert recovery_response.status_code == 409
    assert client.get(f"/studio/api/projects/{project_id}/motion/clips").json() == {"clips": {}}
    too_many_frames = {**CLIP, "frames": [{"spine": 0.0}] * 601, "root_track": [[0, 0, 0]] * 601}
    assert (
        client.put(
            _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": too_many_frames}
        ).status_code
        == 409
    )
    too_many_keys = {
        **CLIP,
        "frames": [{f"bone-{i}": 0.0 for i in range(257)}],
        "root_track": [[0, 0, 0]],
    }
    assert (
        client.put(
            _clip_url(project_id), json={"skeleton_id": "rig_a", "clip": too_many_keys}
        ).status_code
        == 409
    )
    _save_fixture(client, project_id)
    response = client.post(
        f"{_clip_url(project_id)}/retarget",
        json={
            "target_skeleton_id": "rig_b",
            "mapping": {f"bone-{i}": "spine" for i in range(257)},
            "clip_id": "too_mapped",
        },
    )
    assert response.status_code == 409
    long_mapping = client.post(
        f"{_clip_url(project_id)}/retarget",
        json={
            "target_skeleton_id": "rig_b",
            "mapping": {"x" * 129: "spine"},
            "clip_id": "long_mapping",
        },
    )
    assert long_mapping.status_code == 409


def test_choreography_checks_exact_action_count_before_loading_clips(client: TestClient) -> None:
    project_id = _project(client, "Beat count")
    response = client.post(
        f"/studio/api/projects/{project_id}/motion/choreography",
        json={"clip_ids": ["missing"], "clip_id": "dance", "beat_frames": [0]},
    )
    assert response.status_code == 409
    assert "exactly three" in response.json()["detail"]
    long_identifier = client.post(
        f"/studio/api/projects/{project_id}/motion/choreography",
        json={"clip_ids": ["x" * 129, "b", "c"], "clip_id": "dance", "beat_frames": [0, 1, 2]},
    )
    assert long_identifier.status_code == 409


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

    other_clips = client.get(f"/studio/api/projects/{other}/motion/clips").json()["clips"]
    assert other_clips == {}
    assert client.get(_clip_url(other)).status_code == 404

    imported = client.post(
        f"/studio/api/projects/{other}/motion/import",
        json=client.get(f"{_clip_url(project_id)}/export").json(),
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["provenance"]["source_project_id"] == project_id


def test_skeleton_bind_round_trips_through_api(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = _project(client, "Bind pose")
    bound = {**SKELETON_A, "bind": {"spine": 5.0, "arm_L": 10.0}}
    response = client.put(_skeleton_url(project_id), json=bound)
    assert response.status_code == 200, response.text
    assert response.json()["bind"] == {"spine": 5.0, "arm_L": 10.0}
    assert client.put(_skeleton_url(project_id), json=bound).status_code == 200

    legacy = _project(client, "Legacy bind pose")
    assert client.put(_skeleton_url(legacy), json=SKELETON_A).status_code == 200
    assert client.put(_skeleton_url(legacy), json=SKELETON_A).status_code == 200

    fetched = client.get(_skeleton_url(project_id)).json()
    assert fetched["bind"] == {"spine": 5.0, "arm_L": 10.0}

    saved = client.put(_clip_url(project_id), json={"skeleton_id": "rig_a", "clip": CLIP})
    assert saved.status_code == 200, saved.text

    exported_response = client.get(f"{_clip_url(project_id)}/export")
    assert exported_response.status_code == 200, exported_response.text
    exported = exported_response.json()
    assert exported["skeleton"]["bind"] == {"spine": 5.0, "arm_L": 10.0}

    # Bind pose ajena a los huesos declarados se rechaza (validación fail-closed).
    unknown_bone = {**SKELETON_A, "bind": {"no_existe": 1.0}}
    assert client.put(_skeleton_url(project_id, "rig_otro"), json=unknown_bone).status_code == 409

    # Retarget vía API con bind en ambos esqueletos: se transfiere el OFFSET
    # respecto a la bind del fuente y se recorta al rango del destino.
    target = {**SKELETON_B, "bind": {"spine": 0.0, "arm_L": 5.0}}
    assert client.put(_skeleton_url(project_id, "rig_b"), json=target).status_code == 200
    retargeted = client.post(
        f"{_clip_url(project_id)}/retarget",
        json={"target_skeleton_id": "rig_b", "mapping": MAPPING, "clip_id": "walk_b"},
    )
    assert retargeted.status_code == 200, retargeted.text
    frames = retargeted.json()["clip"]["frames"]
    # spine: (10−5)+0=5 y (20−5)+0=15; arm_L: (30−10)+5=25, (200−10)+5→clamp 120
    assert frames[0] == {"spine": 5.0, "arm_L": 25.0}
    assert frames[1] == {"spine": 15.0, "arm_L": 120.0}
    # El esqueleto destino persistido conserva la bind declarada.
    assert client.get(_skeleton_url(project_id, "rig_b")).json()["bind"] == {
        "spine": 0.0,
        "arm_L": 5.0,
    }

    imported_project = _project(client, "Imported bind pose")
    imported = client.post(f"/studio/api/projects/{imported_project}/motion/import", json=exported)
    assert imported.status_code == 200, imported.text
    assert client.get(_skeleton_url(imported_project)).json()["bind"] == {
        "spine": 5.0,
        "arm_L": 10.0,
    }
    assert (
        client.get(_clip_url(imported_project)).json()["content_hash"] == exported["content_hash"]
    )

    # Reinicio: la bind persistida sobrevive al ProjectStore.
    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        assert resumed.get(_skeleton_url(project_id)).json()["bind"] == {
            "spine": 5.0,
            "arm_L": 10.0,
        }
