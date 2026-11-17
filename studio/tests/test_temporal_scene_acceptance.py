"""Aceptación CPU A19 (C06/C17): escena temporal sintética train/held-out.

Regresión de aceptación sobre el API y el ProjectStore reales:

- escena sintética EXPLÍCITA con claves de objeto/pose/cámara (train) y
  consultas en instantes NO muestreados (held-out) contra ground truth
  especificado de forma independiente (constantes a mano, no derivadas del
  código de producto);
- persistencia por proyecto, reencuadre y retiming comparados con ground truth;
- revisita A→B→A medida contra un baseline SIN memoria (sin memoria no existe
  evidencia de conservación de anclas: el round-trip no es medible);
- la procedencia INFERRED nunca se promociona a OBSERVED (ni en fusión, ni tras
  reinicio, ni por re-registro del ancla).

No reclama reconstrucción 4D neuronal ni validación GPU.
"""

from __future__ import annotations

import json
import math
import time

import pytest
from fastapi.testclient import TestClient

from comfy_studio.app import create_app
from comfy_studio.spatial_memory import (
    Provenance,
    SourceRef,
    TemporalScene,
    Timebase,
)

FPS = 24
DURATION_S = 4.0
TRAIN_TIMES = (0.0, 1.0, 2.0, 3.0)
HELD_OUT_TIMES = (0.25, 0.5, 1.5, 2.5, 3.5)
TOLERANCE = 1e-6

# ---------------------------------------------------------------------------
# Escena sintética explícita (train) y ground truth independiente (held-out)
# ---------------------------------------------------------------------------


def _acceptance_scene() -> TemporalScene:
    """Escena explícita: objeto/pose/cámara con anclas A→B→A y un fantasma inferido.

    Train: claves en t ∈ {0, 1, 2, 3}. `crate` y `actor` y `cam` vuelven a su
    ancla A en t=2; `ghost_crate` es geografía INFERIDA sin fuente observada.
    """
    scene = TemporalScene(timebase=Timebase(FPS), duration_s=DURATION_S)
    scene.add_source("shot-01", SourceRef(kind="capture"))
    observed = {"provenance": Provenance.OBSERVED, "source_id": "shot-01"}
    scene.set_object_key("crate", 0.0, (0.0, 0.0, 0.0), **observed)
    scene.set_object_key("crate", 1.0, (2.0, 0.0, 0.0), **observed)
    scene.set_object_key("crate", 2.0, (0.0, 0.0, 0.0), **observed)
    scene.set_object_key("crate", 3.0, (0.0, 0.0, 0.0), **observed)
    scene.set_object_key(
        "ghost_crate", 0.0, (1.0, 1.0, 0.0), provenance=Provenance.INFERRED
    )
    scene.set_object_key(
        "ghost_crate", 1.0, (1.0, 3.0, 0.0), provenance=Provenance.INFERRED
    )
    scene.set_pose_key("actor", 0.0, {"hip": 0.0, "knee": 0.0}, **observed)
    scene.set_pose_key("actor", 1.0, {"hip": 10.0, "knee": 20.0}, **observed)
    scene.set_pose_key("actor", 2.0, {"hip": 0.0, "knee": 0.0}, **observed)
    scene.set_camera_key("cam", 0.0, (0.0, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0, **observed)
    scene.set_camera_key("cam", 1.0, (3.0, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0, **observed)
    scene.set_camera_key("cam", 2.0, (0.0, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0, **observed)
    return scene


# Ground truth especificado a mano, por trozos lineales sobre las claves train.
# NO se deriva del código de producto: cualquier cambio silencioso de muestreo
# rompe estas constantes.
GROUND_TRUTH_OBJECT = {
    "crate": {
        0.25: (0.5, 0.0, 0.0),
        0.5: (1.0, 0.0, 0.0),
        1.5: (1.0, 0.0, 0.0),
        2.5: (0.0, 0.0, 0.0),
        3.5: (0.0, 0.0, 0.0),
    },
    "ghost_crate": {
        0.25: (1.0, 1.5, 0.0),
        0.5: (1.0, 2.0, 0.0),
        1.5: (1.0, 3.0, 0.0),
        2.5: (1.0, 3.0, 0.0),
        3.5: (1.0, 3.0, 0.0),
    },
}

GROUND_TRUTH_POSE = {
    "actor": {
        0.25: {"hip": 2.5, "knee": 5.0},
        0.5: {"hip": 5.0, "knee": 10.0},
        1.5: {"hip": 5.0, "knee": 10.0},
        2.5: {"hip": 0.0, "knee": 0.0},
        3.5: {"hip": 0.0, "knee": 0.0},
    },
}

GROUND_TRUTH_CAMERA = {
    "cam": {
        0.25: ((0.75, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0),
        0.5: ((1.5, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0),
        1.5: ((1.5, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0),
        2.5: ((0.0, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0),
        3.5: ((0.0, 0.0, 4.0), (0.0, 0.0, 0.0), 40.0),
    },
}

# Ground truth de cámara tras reencuadre (Rz 90° + offset (0,0,1)):
# Rz(90°)·(x, y, z) = (-y, x, z); claves pasan a (0,0,5), (0,3,5), (0,0,5).
GROUND_TRUTH_CAMERA_REFRAMED = {
    "cam": {
        0.25: ((0.0, 0.75, 5.0), (0.0, 0.0, 90.0), 40.0),
        0.5: ((0.0, 1.5, 5.0), (0.0, 0.0, 90.0), 40.0),
        1.5: ((0.0, 1.5, 5.0), (0.0, 0.0, 90.0), 40.0),
        2.5: ((0.0, 0.0, 5.0), (0.0, 0.0, 90.0), 40.0),
        3.5: ((0.0, 0.0, 5.0), (0.0, 0.0, 90.0), 40.0),
    },
}

# Tras retime 0.5, el valor en el nuevo instante t' es el valor pre-retime en
# t = 2·t'. Consultas held-out post-retime: t' ∈ {0.25, 0.5, 0.75, 1.25, 1.75}.
GROUND_TRUTH_OBJECT_RETIMED = {
    "crate": {
        0.25: (1.0, 0.0, 0.0),  # τ=0.5
        0.5: (2.0, 0.0, 0.0),  # τ=1.0 (ancla B)
        0.75: (1.0, 0.0, 0.0),  # τ=1.5
        1.25: (0.0, 0.0, 0.0),  # τ=2.5
        1.75: (0.0, 0.0, 0.0),  # τ=3.5
    },
}

GROUND_TRUTH_CAMERA_REFRAMED_RETIMED = {
    "cam": {
        0.25: ((0.0, 1.5, 5.0), (0.0, 0.0, 90.0), 40.0),
        0.5: ((0.0, 3.0, 5.0), (0.0, 0.0, 90.0), 40.0),
        0.75: ((0.0, 1.5, 5.0), (0.0, 0.0, 90.0), 40.0),
        1.25: ((0.0, 0.0, 5.0), (0.0, 0.0, 90.0), 40.0),
        1.75: ((0.0, 0.0, 5.0), (0.0, 0.0, 90.0), 40.0),
    },
}


# ---------------------------------------------------------------------------


def _make_project(client: TestClient, name: str) -> str:
    return client.post("/studio/api/projects", json={"name": name}).json()["id"]


def _put_scene(client: TestClient, project_id: str, scene: TemporalScene):
    return client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=scene.to_dict()
    )


def _sample_object(client: TestClient, project_id: str, entity_id: str, t: float):
    response = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/objects/{entity_id}/sample",
        params={"time": t},
    )
    assert response.status_code == 200, response.text
    return response.json()["position"]


def _sample_pose(client: TestClient, project_id: str, entity_id: str, t: float):
    response = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/poses/{entity_id}/sample",
        params={"time": t},
    )
    assert response.status_code == 200, response.text
    return response.json()["angles"]


def _sample_camera(client: TestClient, project_id: str, entity_id: str, t: float):
    response = client.get(
        f"/studio/api/projects/{project_id}/temporal-scene/cameras/{entity_id}/sample",
        params={"time": t},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    return payload["position"], payload["orientation_deg"], payload["fov_deg"]


def test_train_held_out_scene_matches_independent_ground_truth(client: TestClient) -> None:
    """A19: muestreo held-out vía API contra ground truth independiente."""
    project_id = _make_project(client, "A19 held-out")
    started = time.perf_counter()
    assert _put_scene(client, project_id, _acceptance_scene()).status_code == 200
    payload = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert payload["neural_4d_reconstruction"] == "not_claimed"
    assert payload["gpu_validation"] == "not_run"
    assert payload["objects"]["crate"]["provenance"] == "observed"
    assert payload["objects"]["ghost_crate"]["provenance"] == "inferred"

    worst = 0.0
    for entity_id, truth in GROUND_TRUTH_OBJECT.items():
        for t, expected in truth.items():
            actual = _sample_object(client, project_id, entity_id, t)
            worst = max(worst, max(abs(a - e) for a, e in zip(actual, expected, strict=True)))
            assert actual == pytest.approx(expected, abs=TOLERANCE), (entity_id, t)
    for entity_id, truth in GROUND_TRUTH_POSE.items():
        for t, expected in truth.items():
            actual = _sample_pose(client, project_id, entity_id, t)
            worst = max(
                worst,
                max(abs(actual[bone] - angle) for bone, angle in expected.items()),
            )
            assert actual == pytest.approx(expected, abs=TOLERANCE), (entity_id, t)
    for entity_id, truth in GROUND_TRUTH_CAMERA.items():
        for t, (pos, orient, fov) in truth.items():
            actual_pos, actual_orient, actual_fov = _sample_camera(
                client, project_id, entity_id, t
            )
            worst = max(
                worst,
                max(abs(a - e) for a, e in zip(actual_pos, pos, strict=True)),
                max(abs(a - e) for a, e in zip(actual_orient, orient, strict=True)),
                abs(actual_fov - fov),
            )
            assert actual_pos == pytest.approx(pos, abs=TOLERANCE), (entity_id, t)
            assert actual_orient == pytest.approx(orient, abs=TOLERANCE), (entity_id, t)
            assert actual_fov == pytest.approx(fov, abs=TOLERANCE), (entity_id, t)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # Métricas CPU medidas (se reportan en la ejecución, no se fabrican).
    print(
        f"[A19-metrics] held_out_samples={len(HELD_OUT_TIMES) * 4} "
        f"max_abs_error={worst:.3e} tolerance={TOLERANCE:e} "
        f"elapsed_ms={elapsed_ms:.1f}"
    )
    assert worst <= TOLERANCE


def test_reframe_retime_persist_and_match_ground_truth(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    """A19: reencuadre y retiming deterministas vía API, persistidos y medidos."""
    project_id = _make_project(client, "A19 reframe/retime")
    assert _put_scene(client, project_id, _acceptance_scene()).status_code == 200

    reframe = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/reframe",
        json={"camera_id": "cam", "offset": [0.0, 0.0, 1.0], "rotate_deg": [0.0, 0.0, 90.0]},
    )
    assert reframe.status_code == 200, reframe.text
    for entity_id, truth in GROUND_TRUTH_CAMERA_REFRAMED.items():
        for t, (pos, orient, fov) in truth.items():
            actual_pos, actual_orient, actual_fov = _sample_camera(
                client, project_id, entity_id, t
            )
            assert actual_pos == pytest.approx(pos, abs=TOLERANCE), t
            assert actual_orient == pytest.approx(orient, abs=TOLERANCE), t
            assert actual_fov == pytest.approx(fov, abs=TOLERANCE), t
    # El reencuadre de la cámara no toca objetos ni poses.
    assert _sample_object(client, project_id, "crate", 0.5) == pytest.approx(
        (1.0, 0.0, 0.0), abs=TOLERANCE
    )
    assert _sample_pose(client, project_id, "actor", 0.5) == pytest.approx(
        {"hip": 5.0, "knee": 10.0}, abs=TOLERANCE
    )

    retime = client.post(
        f"/studio/api/projects/{project_id}/temporal-scene/retime",
        json={"scale": 0.5},
    )
    assert retime.status_code == 200, retime.text
    payload = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert payload["duration_s"] == pytest.approx(2.0)
    assert payload["timebase"]["fps"] == FPS
    assert [k["t"] for k in payload["objects"]["crate"]["keys"]] == [0.0, 0.5, 1.0, 1.5]
    for entity_id, truth in GROUND_TRUTH_OBJECT_RETIMED.items():
        for t, expected in truth.items():
            assert _sample_object(client, project_id, entity_id, t) == pytest.approx(
                expected, abs=TOLERANCE
            ), t
    for entity_id, truth in GROUND_TRUTH_CAMERA_REFRAMED_RETIMED.items():
        for t, (pos, orient, fov) in truth.items():
            actual_pos, actual_orient, actual_fov = _sample_camera(
                client, project_id, entity_id, t
            )
            assert actual_pos == pytest.approx(pos, abs=TOLERANCE), t
            assert actual_orient == pytest.approx(orient, abs=TOLERANCE), t
            assert actual_fov == pytest.approx(fov, abs=TOLERANCE), t

    # Persistencia: un nuevo proceso sobre el mismo store recupera el estado
    # reencuadrado y retemporizado.
    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        restored = resumed.get(
            f"/studio/api/projects/{project_id}/temporal-scene"
        ).json()
        assert restored["duration_s"] == pytest.approx(2.0)
        assert [k["t"] for k in restored["objects"]["crate"]["keys"]] == [
            0.0,
            0.5,
            1.0,
            1.5,
        ]
        assert restored["cameras"]["cam"]["keys"][1]["position"] == pytest.approx(
            [0.0, 3.0, 5.0]
        )
        assert restored["objects"]["ghost_crate"]["provenance"] == "inferred"


def test_round_trip_revisit_with_no_memory_baseline(client: TestClient) -> None:
    """A19: la revisita A→B→A se mide CON memoria; sin memoria no hay evidencia."""
    memory_project = _make_project(client, "A19 con memoria")
    baseline_project = _make_project(client, "A19 baseline sin memoria")

    # Baseline: un store sin memoria registrada no puede afirmar conservación.
    baseline_memory = client.get(
        f"/studio/api/projects/{baseline_project}/spatial-memory"
    ).json()
    assert baseline_memory["anchors"] == {}
    baseline_report = client.get(
        f"/studio/api/projects/{baseline_project}/spatial-memory/anchors/ancla_mesa/round-trip"
    )
    assert baseline_report.status_code == 409
    assert "round-trip" in baseline_report.json()["detail"]

    # Con memoria: la revisita vuelve al ancla exacta; drift medido (0).
    for anchor_id, provenance in (("ancla_mesa", "observed"), ("ancla_farola", "observed")):
        registered = client.put(
            f"/studio/api/projects/{memory_project}/spatial-memory/anchors/{anchor_id}",
            json={"provenance": provenance},
        )
        assert registered.status_code == 200, registered.text

    def visit(project: str, anchor: str, shot: str, position: list[float]) -> None:
        response = client.post(
            f"/studio/api/projects/{project}/spatial-memory/anchors/{anchor}/visits",
            json={"shot_id": shot, "position": position},
        )
        assert response.status_code == 200, response.text

    visit(memory_project, "ancla_mesa", "A", [10.0, 0.0, 5.0])
    visit(memory_project, "ancla_mesa", "B", [20.0, 0.0, 5.0])
    visit(memory_project, "ancla_mesa", "A2", [10.0, 0.0, 5.0])
    report = client.get(
        f"/studio/api/projects/{memory_project}/spatial-memory/anchors/ancla_mesa/round-trip"
    ).json()
    assert report["shots"] == ["A", "B", "A2"]
    assert report["drift"] == pytest.approx(0.0)
    assert report["returned"] is True

    # Drift honesto: una revisita que NO vuelve exacto se registra como drift.
    visit(memory_project, "ancla_farola", "A", [0.0, 0.0, 0.0])
    visit(memory_project, "ancla_farola", "B", [1.0, 0.0, 0.0])
    visit(memory_project, "ancla_farola", "A2", [0.4, 0.0, 0.0])
    drifting = client.get(
        f"/studio/api/projects/{memory_project}/spatial-memory/anchors/ancla_farola/round-trip"
    ).json()
    assert drifting["drift"] == pytest.approx(0.4)
    assert drifting["returned"] is False

    # La diferencia contra el baseline es medible: la memoria aporta la métrica
    # (drift/returned) que el store sin memoria no puede producir.
    memory_payload = client.get(
        f"/studio/api/projects/{memory_project}/spatial-memory"
    ).json()
    assert set(memory_payload["drift"]) == {"ancla_mesa", "ancla_farola"}
    assert memory_payload["drift"]["ancla_mesa"] == pytest.approx(0.0)


def test_inferred_provenance_never_upgrades_to_observed(
    client: TestClient,
    studio_config,
    stub_comfyui_transport,
) -> None:
    """A19: la geografía inferida permanece inferida en fusión, reinicio y memoria."""
    project_id = _make_project(client, "A19 procedencia")
    scene = _acceptance_scene()
    assert _put_scene(client, project_id, scene).status_code == 200

    # Fusión: una escritura que reclama la entidad inferida como observada falla.
    claimed = scene.to_dict()
    claimed["objects"]["ghost_crate"]["provenance"] = "observed"
    conflict = client.put(
        f"/studio/api/projects/{project_id}/temporal-scene", json=claimed
    )
    assert conflict.status_code == 409
    saved = client.get(f"/studio/api/projects/{project_id}/temporal-scene").json()
    assert saved["objects"]["ghost_crate"]["provenance"] == "inferred"
    assert saved["objects"]["ghost_crate"]["source_id"] is None
    # La muestra inferida sigue disponible, marcada como inferida (no factual).
    assert _sample_object(client, project_id, "ghost_crate", 0.5) == pytest.approx(
        (1.0, 2.0, 0.0), abs=TOLERANCE
    )

    # Reinicio del proceso: la procedencia inferida sobrevive.
    restarted = create_app(config=studio_config, comfyui_transport=stub_comfyui_transport)
    with TestClient(restarted) as resumed:
        resumed.auth = ("admin", "changeme-test-pass-42")
        restored = resumed.get(
            f"/studio/api/projects/{project_id}/temporal-scene"
        ).json()
        assert restored["objects"]["ghost_crate"]["provenance"] == "inferred"

        # Memoria espacial: un ancla inferida no se re-registra como observada.
        inferred_anchor = resumed.put(
            f"/studio/api/projects/{project_id}/spatial-memory/anchors/ancla_inferida",
            json={"provenance": "inferred"},
        )
        assert inferred_anchor.status_code == 200, inferred_anchor.text
        upgrade = resumed.put(
            f"/studio/api/projects/{project_id}/spatial-memory/anchors/ancla_inferida",
            json={"provenance": "observed"},
        )
        assert upgrade.status_code == 409
        memory = resumed.get(
            f"/studio/api/projects/{project_id}/spatial-memory"
        ).json()
        assert memory["anchors"]["ancla_inferida"]["provenance"] == "inferred"


def test_scene_serialization_survives_artifact_round_trip(tmp_path) -> None:
    """La escena de aceptación serializa de forma determinista y se recarga igual."""
    scene = _acceptance_scene()
    artifact = tmp_path / "a19-acceptance-scene.json"
    artifact.write_text(scene.dumps(), encoding="utf-8")
    data = json.loads(artifact.read_text(encoding="utf-8"))
    assert data["schema"] == "temporal-scene/1"
    restored = TemporalScene.from_dict(data)
    assert restored.dumps() == scene.dumps()
    assert restored.object_round_trip("crate")["returned"] is True
    assert restored.pose_round_trip("actor")["returned"] is True
    assert restored.camera_round_trip("cam")["returned"] is True
    assert math.isfinite(restored.duration_s)
