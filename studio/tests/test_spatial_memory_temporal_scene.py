"""Escena temporal editable por proyecto (plan Ultra Task 1, C06/C17 / A19).

Representación editable acotada indexada por tiempo de objetos/poses/cámaras,
con timebase estricta, serialización determinista, reframe/retime, procedencia
de fuentes y fixture sintético CPU A→B→A con consulta held-out. NO es
reconstrucción 4D neuronal: es una representación de keyframes editable.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from comfy_studio.spatial_memory import (
    Provenance,
    SourceRef,
    SpatialError,
    TemporalScene,
    Timebase,
    synthetic_round_trip_fixture,
)

# ---------------------------------------------------------------------------
# Timebase estricta
# ---------------------------------------------------------------------------


def test_timebase_requires_positive_bounded_integer_fps() -> None:
    assert Timebase(fps=24).fps == 24
    for bad in (0, -24, 241, 24.0, True):
        with pytest.raises(SpatialError, match="fps"):
            Timebase(fps=bad)


def test_timebase_change_validates() -> None:
    scene = synthetic_round_trip_fixture()
    scene.set_timebase(30)
    assert scene.timebase.fps == 30
    with pytest.raises(SpatialError, match="fps"):
        scene.set_timebase(0)


# ---------------------------------------------------------------------------
# Fixture sintético A→B→A (CPU) + consulta held-out
# ---------------------------------------------------------------------------


def test_fixture_round_trip_conserves_anchor_with_measured_drift() -> None:
    """A19: A→B→A conserva el ancla; el drift se registra (aquí, 0)."""
    scene = synthetic_round_trip_fixture()
    report = scene.object_round_trip("mesa")
    assert report["entity_id"] == "mesa"
    assert report["drift"] == 0.0
    assert report["returned"] is True
    assert [k.t for k in scene.objects["mesa"].keys] == [0.0, 1.0, 2.0, 2.5]
    assert all(entity.provenance is Provenance.SYNTHETIC for entity in scene.objects.values())
    assert all(entity.provenance is Provenance.SYNTHETIC for entity in scene.poses.values())
    assert all(entity.provenance is Provenance.SYNTHETIC for entity in scene.cameras.values())


def test_fixture_drift_is_measured_when_object_does_not_return() -> None:
    scene = synthetic_round_trip_fixture()
    scene.set_object_key("farola", 0.0, (0.0, 0.0, 0.0))
    scene.set_object_key("farola", 1.0, (1.0, 0.0, 0.0))
    scene.set_object_key("farola", 2.0, (0.3, 0.0, 0.0))  # no vuelve exacto
    report = scene.object_round_trip("farola")
    assert report["drift"] == pytest.approx(0.3)
    assert report["returned"] is False


def test_held_out_query_interpolates_between_keys() -> None:
    """Consulta en un instante NO muestreado (held-out) con ground truth."""
    scene = synthetic_round_trip_fixture()
    # A=(0,0,0) en t=0, B=(2,0,0) en t=1: en t=0.5 el punto medio es (1,0,0).
    assert scene.sample_object("mesa", 0.5) == pytest.approx((1.0, 0.0, 0.0))
    # Fuera del rango muestreado se mantiene el último valor (A en t=2.5).
    assert scene.sample_object("mesa", 3.0) == pytest.approx((0.0, 0.0, 0.0))


def test_held_out_pose_and_camera_queries() -> None:
    scene = synthetic_round_trip_fixture()
    pose = scene.sample_pose("actor_1", 0.5)
    assert pose["hip"] == pytest.approx(2.5)  # 0.0 -> 5.0 lineal
    camera = scene.sample_camera("cam", 0.5)
    assert camera.position == pytest.approx((1.0, 1.0, 2.0))  # y: 0 -> 2 lineal


def test_sample_unknown_entity_fails() -> None:
    scene = synthetic_round_trip_fixture()
    with pytest.raises(SpatialError, match="no registrada"):
        scene.sample_object("fantasma", 0.5)


# ---------------------------------------------------------------------------
# Reframe / retime contra ground truth
# ---------------------------------------------------------------------------


def test_reframe_matches_ground_truth() -> None:
    """A19: reencuadre del fixture desde la representación, contra ground truth."""
    scene = synthetic_round_trip_fixture()
    before = {k.t: k.position for k in scene.cameras["cam"].keys}
    assert before == {0.0: (1.0, 0.0, 2.0), 1.0: (1.0, 2.0, 2.0)}

    scene.reframe("cam", offset=(0.0, 0.0, 1.0), rotate_deg=(0.0, 0.0, 90.0))

    after = {k.t: k.position for k in scene.cameras["cam"].keys}
    # Rz(90°)·(x,y,z) = (-y, x, z), luego + offset (0,0,1).
    assert after[0.0] == pytest.approx((0.0, 1.0, 3.0))
    assert after[1.0] == pytest.approx((-2.0, 1.0, 3.0))
    orient = {k.t: k.orientation_deg for k in scene.cameras["cam"].keys}
    assert orient[0.0] == pytest.approx((0.0, 0.0, 90.0))
    # El reframe de la cámara no toca los objetos.
    assert scene.sample_object("mesa", 0.5) == pytest.approx((1.0, 0.0, 0.0))


def test_reframe_rejects_invalid_camera_or_values() -> None:
    scene = synthetic_round_trip_fixture()
    with pytest.raises(SpatialError, match="no registrada"):
        scene.reframe("fantasma", offset=(1, 0, 0))
    with pytest.raises(SpatialError, match="finito"):
        scene.reframe("cam", offset=(float("nan"), 0.0, 0.0))


def test_retime_matches_ground_truth() -> None:
    scene = synthetic_round_trip_fixture()
    scene.retime(0.5)
    times = [k.t for k in scene.objects["mesa"].keys]
    assert times == [0.0, 0.5, 1.0, 1.25]
    # El valor que estaba en t=1.0 (B) ahora vive en t=0.5.
    assert scene.sample_object("mesa", 0.5) == pytest.approx((2.0, 0.0, 0.0))
    assert scene.duration_s == pytest.approx(1.5)
    # El drift del round-trip no cambia al retemporizar.
    assert scene.object_round_trip("mesa")["returned"] is True


def test_retime_rejects_invalid_scale() -> None:
    scene = synthetic_round_trip_fixture()
    for bad in (0.0, -2.0, float("inf"), float("nan")):
        with pytest.raises(SpatialError, match="escala"):
            scene.retime(bad)


# ---------------------------------------------------------------------------
# Timebase / límites / escrituras conflictivas
# ---------------------------------------------------------------------------


def test_non_finite_or_negative_times_rejected() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    with pytest.raises(SpatialError, match="tiempo"):
        scene.set_object_key("a", float("nan"), (0, 0, 0))
    with pytest.raises(SpatialError, match="tiempo"):
        scene.set_object_key("a", -1.0, (0, 0, 0))
    with pytest.raises(SpatialError, match="tiempo"):
        scene.set_object_key("a", 5.0, (0, 0, 0))  # fuera de duration_s


def test_non_finite_positions_rejected() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    with pytest.raises(SpatialError, match="finito"):
        scene.set_object_key("a", 0.0, (0.0, float("inf"), 0.0))
    with pytest.raises(SpatialError, match="finito"):
        scene.set_pose_key("p", 0.0, {"hip": float("nan")})


def test_conflicting_write_at_same_time_fails_closed() -> None:
    """Reintentos idempotentes; escrituras conflictivas no reemplazan en silencio."""
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    scene.set_object_key("a", 1.0, (1.0, 2.0, 3.0))
    scene.set_object_key("a", 1.0, (1.0, 2.0, 3.0))  # reintento idempotente
    assert len(scene.objects["a"].keys) == 1
    with pytest.raises(SpatialError, match="conflicto"):
        scene.set_object_key("a", 1.0, (9.0, 2.0, 3.0))


def test_keys_stay_sorted_by_time() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    scene.set_object_key("a", 2.0, (0, 0, 0))
    scene.set_object_key("a", 0.0, (0, 0, 0))
    scene.set_object_key("a", 1.0, (0, 0, 0))
    assert [k.t for k in scene.objects["a"].keys] == [0.0, 1.0, 2.0]


def test_entity_and_key_limits_respected() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0, max_entities=2)
    scene.set_object_key("a", 0.0, (0, 0, 0))
    scene.set_camera_key("cam", 0.0, (0, 0, 5), (0, 0, 0))
    with pytest.raises(SpatialError, match="límite"):
        scene.set_object_key("b", 0.0, (0, 0, 0))

    bounded = TemporalScene(timebase=Timebase(24), duration_s=3.0, max_keys_per_entity=2)
    bounded.set_object_key("a", 0.0, (0, 0, 0))
    bounded.set_object_key("a", 1.0, (0, 0, 0))
    with pytest.raises(SpatialError, match="límite"):
        bounded.set_object_key("a", 2.0, (0, 0, 0))


def test_source_ids_must_exist_in_sources() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    with pytest.raises(SpatialError, match="fuente"):
        scene.set_object_key("a", 0.0, (0, 0, 0), source_id="inexistente")
    scene.add_source("cap-1", SourceRef(kind="shot"))
    scene.set_object_key("a", 0.0, (0, 0, 0), source_id="cap-1")
    assert scene.objects["a"].source_id == "cap-1"
    with pytest.raises(SpatialError, match="procedencia inmutable"):
        scene.set_object_key("a", 1.0, (1, 0, 0))
    scene.add_source("cap-2", SourceRef(kind="shot"))
    with pytest.raises(SpatialError, match="procedencia inmutable"):
        scene.set_object_key("a", 1.0, (1, 0, 0), source_id="cap-2")

    synthetic = TemporalScene(timebase=Timebase(24), duration_s=1.0)
    synthetic.add_source("fixture", SourceRef(kind="synthetic"))
    with pytest.raises(SpatialError, match="no coincide con fuente sintética"):
        synthetic.set_object_key("obj", 0.0, (0, 0, 0), source_id="fixture")


def test_invalid_query_time_and_camera_fov_fail_as_spatial_errors() -> None:
    scene = synthetic_round_trip_fixture()
    for invalid_time in (None, "bad", True):
        with pytest.raises(SpatialError, match="tiempo de consulta"):
            scene.sample_object("mesa", invalid_time)
    with pytest.raises(SpatialError, match="fov_deg"):
        scene.set_camera_key("other-camera", 0.0, (0, 0, 0), (0, 0, 0), None)


def test_source_ref_validates_kind_and_sha256() -> None:
    with pytest.raises(SpatialError, match="kind"):
        SourceRef(kind="")
    with pytest.raises(SpatialError, match="sha256"):
        SourceRef(kind="shot", sha256="no-es-un-hash")
    ref = SourceRef(kind="synthetic", sha256="a" * 64)
    assert ref.sha256 == "a" * 64


def test_entity_ids_unique_across_kinds() -> None:
    scene = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    scene.set_object_key("duplicado", 0.0, (0, 0, 0))
    with pytest.raises(SpatialError, match="duplicado"):
        scene.set_camera_key("duplicado", 0.0, (0, 0, 5), (0, 0, 0))


# ---------------------------------------------------------------------------
# Serialización determinista
# ---------------------------------------------------------------------------


def test_serialization_is_deterministic_regardless_of_insertion_order() -> None:
    s1 = synthetic_round_trip_fixture()
    # Misma escena, otra orden de inserción (cámara primero, claves en desorden).
    s2 = TemporalScene(timebase=Timebase(24), duration_s=3.0)
    s2.add_source("cap-synthetic", s1.sources["cap-synthetic"])
    s2.set_camera_key(
        "cam", 1.0, (1.0, 2.0, 2.0), (0.0, 0.0, 0.0), 50.0,
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_camera_key(
        "cam", 0.0, (1.0, 0.0, 2.0), (0.0, 0.0, 0.0), 50.0,
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_object_key(
        "mesa", 1.0, (2.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_object_key(
        "mesa", 0.0, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_object_key(
        "mesa", 2.5, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_object_key(
        "mesa", 2.0, (0.0, 0.0, 0.0),
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_pose_key(
        "actor_1", 2.0, {"knee": 0.0, "hip": 0.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_pose_key(
        "actor_1", 0.0, {"hip": 0.0, "knee": 0.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )
    s2.set_pose_key(
        "actor_1", 1.0, {"hip": 5.0, "knee": 10.0},
        provenance=Provenance.SYNTHETIC, source_id="cap-synthetic",
    )

    assert s1.dumps() == s2.dumps()


def test_round_trip_serialization_is_lossless_and_deterministic() -> None:
    scene = synthetic_round_trip_fixture()
    restored = TemporalScene.from_dict(scene.to_dict())
    assert restored.dumps() == scene.dumps()


def test_serialization_is_valid_json_with_sorted_keys() -> None:
    scene = synthetic_round_trip_fixture()
    payload = scene.dumps()
    data = json.loads(payload)
    assert payload == json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    assert data["schema"] == "temporal-scene/1"


def test_unknown_schema_rejected() -> None:
    with pytest.raises(SpatialError, match="schema"):
        TemporalScene.from_dict({"schema": "temporal-scene/999"})


# ---------------------------------------------------------------------------
# Persistencia por proyecto vía API
# ---------------------------------------------------------------------------


def _make_project(client: TestClient, name: str) -> str:
    return client.post("/studio/api/projects", json={"name": name}).json()["id"]


def test_temporal_scene_persists_and_resumes_after_app_restart(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    project_id = _make_project(client, "Escena temporal")
    scene = synthetic_round_trip_fixture()
    put = client.put(
        f"/studio/api/projects/{project_id}/temporal-scene",
        json=scene.to_dict(),
    )
    assert put.status_code == 200, put.text
    before = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert before["neural_4d_reconstruction"] == "not_claimed"
    assert before["gpu_validation"] == "not_run"

    from comfy_studio.app import create_app

    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        after = resumed.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
        assert {k: v for k, v in after.items() if not k.startswith(("neural_", "gpu_"))} == {
            k: v for k, v in before.items() if not k.startswith(("neural_", "gpu_"))
        }


def test_temporal_scene_fails_closed_for_other_project(client: TestClient) -> None:
    assert (
        client.get("/studio/api/projects/no-existe/temporal-scene").status_code == 404
    )
    assert (
        client.put(
            "/studio/api/projects/no-existe/temporal-scene",
            json=synthetic_round_trip_fixture().to_dict(),
        ).status_code
        == 404
    )


def test_temporal_scene_rejects_invalid_payload(client: TestClient) -> None:
    project_id = _make_project(client, "Escena inválida")
    bad = synthetic_round_trip_fixture().to_dict()
    bad["timebase"]["fps"] = 0
    response = client.put(f"/studio/api/projects/{project_id}/temporal-scene", json=bad)
    assert response.status_code == 409
    # El estado guardado no cambia con un payload inválido.
    scene = synthetic_round_trip_fixture()
    assert client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=scene.to_dict()
    ).status_code == 200
    conflict = scene.to_dict()
    conflict["objects"]["mesa"]["keys"][0]["position"] = [9.0, 9.0, 9.0]
    response = client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=conflict
    )
    assert response.status_code == 409
    saved = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert saved["objects"]["mesa"]["keys"][0]["position"] == [0.0, 0.0, 0.0]


def test_temporal_scene_rejects_malformed_json_and_conflicting_fps(client: TestClient) -> None:
    project_id = _make_project(client, "Escena JSON/fps")
    url = f"/studio/api/projects/{project_id}/temporal-scene"
    malformed = client.put(url, content=b"{", headers={"content-type": "application/json"})
    assert malformed.status_code == 400

    scene = synthetic_round_trip_fixture()
    assert client.put(url, json=scene.to_dict()).status_code == 200
    incoming = synthetic_round_trip_fixture().to_dict()
    incoming["timebase"]["fps"] = 30
    conflict = client.put(url, json=incoming)
    assert conflict.status_code == 409
    saved = client.get(url).json()
    assert saved["timebase"]["fps"] == 24


def test_temporal_scene_reframe_and_retime_via_api(client: TestClient) -> None:
    project_id = _make_project(client, "Reframe API")
    scene = synthetic_round_trip_fixture()
    assert client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=scene.to_dict()
    ).status_code == 200

    reframe = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/reframe",
        json={"camera_id": "cam", "offset": [0.0, 0.0, 1.0], "rotate_deg": [0.0, 0.0, 90.0]},
    )
    assert reframe.status_code == 200, reframe.text
    saved = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert saved["cameras"]["cam"]["keys"][0]["position"] == [0.0, 1.0, 3.0]

    retime = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/retime",
        json={"scale": 0.5, "new_fps": 12},
    )
    assert retime.status_code == 200, retime.text
    saved = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert saved["timebase"]["fps"] == 12
    assert [k["t"] for k in saved["objects"]["mesa"]["keys"]] == [0.0, 0.5, 1.0, 1.25]

    bad_retime = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/retime",
        json={"scale": -1.0},
    )
    assert bad_retime.status_code == 409


def test_temporal_scene_round_trip_and_sample_endpoints(client: TestClient) -> None:
    project_id = _make_project(client, "Round-trip API")
    scene = synthetic_round_trip_fixture()
    assert client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=scene.to_dict()
    ).status_code == 200

    report = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/objects/mesa/round-trip"
    )
    assert report.status_code == 200, report.text
    assert report.json()["drift"] == 0.0
    assert report.json()["returned"] is True

    sample = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/objects/mesa/sample",
        params={"time": 0.5},
    )
    assert sample.status_code == 200, sample.text
    assert sample.json()["position"] == pytest.approx([1.0, 0.0, 0.0])

    missing = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/objects/fantasma/round-trip"
    )
    assert missing.status_code == 404
