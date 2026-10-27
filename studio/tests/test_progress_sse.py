"""Tests para el canal SSE /studio/api/progress (eventos de job + heartbeat sin buffering)."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _parse_sse_events(raw_text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in raw_text.strip().split("\n\n"):
        if not block.strip():
            continue
        event_type = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].strip())
        if data_lines:
            payload = json.loads("\n".join(data_lines))
            events.append((event_type, payload))
    return events


def test_sse_progress_emits_heartbeat_and_no_buffering_headers(
    client: TestClient,
) -> None:
    resp = client.get("/studio/api/progress?max_events=2&heartbeat_interval=0.02")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.headers.get("x-accel-buffering") == "no"
    assert "no-cache" in resp.headers.get("cache-control", "")

    events = _parse_sse_events(resp.text)
    assert len(events) == 2
    assert all(ev[0] == "heartbeat" for ev in events)
    assert events[0][1]["type"] == "heartbeat"


def test_sse_progress_emits_job_and_progress_events(
    client: TestClient,
) -> None:
    # Crear un proyecto y un job, luego actualizar su progreso
    proj = client.post("/studio/api/projects", json={"name": "SSE Job Project"}).json()
    job_resp = client.post(
        "/studio/api/jobs",
        json={
            "project_id": proj["id"],
            "kind": "render",
            "template_id": "wan_t2v",
            "params": {"prompt": "un faro en tormenta"},
        },
    )
    assert job_resp.status_code in (200, 201)
    job = job_resp.json()

    prog_resp = client.post(
        f"/studio/api/jobs/{job['id']}/progress",
        json={
            "status": "running",
            "progress_value": 10,
            "progress_max": 20,
            "current_node": "KSampler",
        },
    )
    assert prog_resp.status_code == 200

    # Suscribirse con replay=1 para recibir los eventos recientes del job + heartbeat inicial
    sse_resp = client.get("/studio/api/progress?replay=1&max_events=3")
    assert sse_resp.status_code == 200
    events = _parse_sse_events(sse_resp.text)
    event_types = [ev[0] for ev in events]
    assert "heartbeat" in event_types
    assert "job" in event_types or "progress" in event_types
    job_payloads = [ev[1] for ev in events if ev[0] in ("job", "progress")]
    assert any(p.get("job_id") == job["id"] for p in job_payloads)
