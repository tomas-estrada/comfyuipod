"""Tests de review pins (M3-RVP-001): schema v2, CRUD, endpoints y dominio.

Cubre la migración v1→v2, el ciclo de vida de un pin (open → resolved/discarded),
la validación de ventanas y severidades, el emparejamiento pin↔corte y el
resumen por severidad para el contexto del agente.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.projects import SCHEMA_VERSION, ProjectStore
from comfy_studio.review_pins import (
    ReviewPinError,
    match_pin_to_cut,
    open_pin_summary,
    validate_pin_window,
)
from comfy_studio.webhooks import map_broker_event


def _create_edl_version(client: TestClient, project_id: str, version: int = 1) -> int:
    resp = client.post(
        f"/studio/api/projects/{project_id}/edl",
        json={
            "title": "EDL Review",
            "aspect_ratio": "16:9",
            "cuts": [{"in_sec": 0.0, "out_sec": 5.0, "reason": "highlight"}],
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["version"] == version
    return int(created["version"])


def _pin_payload(
    edl_version: int,
    t_start: float = 1.0,
    t_end: float = 2.0,
    severity: str = "issue",
    body: str = "Corte muy brusco, suavizar.",
) -> dict:
    return {
        "edl_version": edl_version,
        "t_start": t_start,
        "t_end": t_end,
        "severity": severity,
        "body": body,
    }


def test_schema_v2_review_pins_table(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    # El esquema ha crecido (v3 añade render_measurements), pero la tabla de
    # review_pins y su migración siguen siendo las de v2.
    assert SCHEMA_VERSION >= 2
    assert store.get_schema_version() == SCHEMA_VERSION
    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "review_pins" in tables
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == SCHEMA_VERSION


def test_review_pins_endpoint_crud_roundtrip(client: TestClient) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Pins CRUD"}
    ).json()["id"]
    edl_version = _create_edl_version(client, project_id)

    created = client.post(
        f"/studio/api/projects/{project_id}/pins",
        json=_pin_payload(edl_version),
    )
    assert created.status_code == 201, created.text
    pin = created.json()
    assert pin["status"] == "open"
    assert pin["author"] == "reviewer"
    assert pin["severity"] == "issue"
    assert pin["resolved_by"] is None
    pin_id = pin["id"]

    listed = client.get(
        f"/studio/api/projects/{project_id}/pins",
        params={"edl_version": edl_version, "status": "open"},
    )
    assert listed.status_code == 200
    assert listed.json()["count"] == 1

    summary = client.get(
        f"/studio/api/projects/{project_id}/pins/summary",
        params={"edl_version": edl_version},
    )
    assert summary.status_code == 200
    assert summary.json()["summary"] == {"note": 0, "issue": 1, "blocker": 0, "open": 1}

    patched = client.patch(
        f"/studio/api/projects/{project_id}/pins/{pin_id}",
        json={"status": "resolved", "resolution_note": "Regenerado como v2"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "resolved"
    assert patched.json()["resolved_by"] == "reviewer"

    deleted = client.delete(f"/studio/api/projects/{project_id}/pins/{pin_id}")
    assert deleted.status_code == 409  # solo pins abiertos se eliminan

    second = client.post(
        f"/studio/api/projects/{project_id}/pins",
        json=_pin_payload(edl_version, body="otro"),
    ).json()
    ok_delete = client.delete(
        f"/studio/api/projects/{project_id}/pins/{second['id']}"
    )
    assert ok_delete.status_code == 200
    assert ok_delete.json()["deleted"] is True


def test_review_pin_validation_errors(client: TestClient) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Pins Validación"}
    ).json()["id"]
    edl_version = _create_edl_version(client, project_id)

    missing_version = client.post(
        f"/studio/api/projects/{project_id}/pins",
        json=_pin_payload(99),
    )
    assert missing_version.status_code == 404

    bad_window = client.post(
        f"/studio/api/projects/{project_id}/pins",
        json=_pin_payload(edl_version, t_start=3.0, t_end=1.0),
    )
    assert bad_window.status_code == 422  # validador del modelo Pydantic

    unknown_project = client.post(
        "/studio/api/projects/proj-inexistente/pins",
        json=_pin_payload(edl_version),
    )
    assert unknown_project.status_code == 404

    bad_status = client.get(
        f"/studio/api/projects/{project_id}/pins",
        params={"status": "bogus"},
    )
    assert bad_status.status_code == 422


def test_review_pin_cascade_on_project_delete(client: TestClient) -> None:
    project_id = client.post(
        "/studio/api/projects", json={"name": "Pins Cascade"}
    ).json()["id"]
    edl_version = _create_edl_version(client, project_id)
    client.post(
        f"/studio/api/projects/{project_id}/pins",
        json=_pin_payload(edl_version),
    )
    assert client.delete(f"/studio/api/projects/{project_id}").status_code in (200, 204)
    gone = client.get(f"/studio/api/projects/{project_id}/pins")
    assert gone.status_code == 404


def test_validate_pin_window() -> None:
    validate_pin_window(0.0, 1.5, timeline_duration=10.0)
    with pytest.raises(ReviewPinError):
        validate_pin_window(-1.0, 1.0)
    with pytest.raises(ReviewPinError):
        validate_pin_window(2.0, 1.0)
    with pytest.raises(ReviewPinError):
        validate_pin_window(12.0, 13.0, timeline_duration=10.0)
    validate_pin_window(12.0, 13.0, timeline_duration=None)


def test_match_pin_to_cut() -> None:
    cuts = [
        {"in_sec": 0.0, "out_sec": 5.0, "reason": "highlight"},
        {"in_sec": 5.0, "out_sec": 10.0, "reason": "highlight"},
    ]
    inside = {"t_start": 1.0, "t_end": 2.0}
    assert match_pin_to_cut(inside, cuts) is cuts[0]
    spanning = {"t_start": 4.0, "t_end": 8.0}
    assert match_pin_to_cut(spanning, cuts) is cuts[1]
    gap = {"t_start": 12.0, "t_end": 13.0}
    assert match_pin_to_cut(gap, cuts) is None
    assert match_pin_to_cut(inside, []) is None


def test_open_pin_summary() -> None:
    pins = [
        {"severity": "issue", "status": "open"},
        {"severity": "blocker", "status": "open"},
        {"severity": "issue", "status": "open"},
        {"severity": "note", "status": "resolved"},
    ]
    assert open_pin_summary(pins) == {
        "note": 0,
        "issue": 2,
        "blocker": 1,
        "open": 3,
    }
    assert open_pin_summary([]) == {"note": 0, "issue": 0, "blocker": 0, "open": 0}


def test_webhook_map_review_pin_event() -> None:
    base = {
        "project_id": "p1",
        "pin_id": "pin-1",
        "edl_version": 1,
        "t_start": 1.0,
        "t_end": 2.0,
        "severity": "issue",
        "status": "open",
    }
    added = map_broker_event("review_pin", {**base, "action": "added"})
    assert added is not None
    assert added[0] == "review_pin_added"
    assert added[1]["pin_id"] == "pin-1"
    resolved = map_broker_event(
        "review_pin", {**base, "status": "resolved", "action": "resolved"}
    )
    assert resolved is not None
    assert resolved[0] == "review_pin_resolved"
    assert map_broker_event("review_pin", {**base, "action": "bogus"}) is None
    assert map_broker_event("review_pin", base) is None
