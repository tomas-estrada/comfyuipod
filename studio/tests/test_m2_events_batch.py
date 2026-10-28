"""Tests de los eventos de webhook M2 y del render por lotes multi-aspecto.

Cubre `M3-IDEA-034` (`transcription_done`, `edl_rendered`, `edl_proposed`) y
`M3-IDEA-036` (`POST edl/build` con `aspect_ratios`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.webhooks import map_broker_event


@pytest.mark.parametrize(
    ("event_type", "data", "expected_event"),
    [
        (
            "transcription",
            {
                "project_id": "p1",
                "transcription_id": "tr-1",
                "asset_id": "a1",
                "language": "es",
                "segments_count": 3,
                "words_count": 12,
                "diarized": True,
                "backend": "whisperx",
                "simulated": True,
            },
            "transcription_done",
        ),
        (
            "edl",
            {
                "project_id": "p1",
                "edl_id": "e1",
                "version": 2,
                "aspect_ratio": "16:9",
                "output_asset_id": "out1",
                "status": "built",
            },
            "edl_rendered",
        ),
        (
            "edl",
            {
                "project_id": "p1",
                "edl_id": "e1",
                "version": 2,
                "aspect_ratio": "9:16",
                "total_duration_sec": 12.5,
                "cuts_count": 4,
                "status": "proposed",
            },
            "edl_proposed",
        ),
    ],
)
def test_map_broker_event_m2_events(
    event_type: str, data: dict, expected_event: str
) -> None:
    mapped = map_broker_event(event_type, data)
    assert mapped is not None
    assert mapped[0] == expected_event
    # El payload nunca incluye campos internos no listados.
    assert set(mapped[1]).issubset(set(data))


def test_map_broker_event_ignores_unknown_edl_status() -> None:
    assert map_broker_event("edl", {"status": "queued"}) is None
    assert map_broker_event("transcription", {"project_id": "p1"}) is not None
    assert map_broker_event("unknown", {}) is None


def _segments() -> list[dict]:
    return [
        {"start": 0.0, "end": 1.5, "text": "uno", "speaker": "SPEAKER_00"},
        {"start": 1.5, "end": 3.0, "text": "dos", "speaker": "SPEAKER_01"},
    ]


def test_transcribe_endpoint_publishes_transcription_event(client: TestClient) -> None:
    """El evento `transcription` alimenta SSE y `transcription_done` en webhooks."""
    project_id = client.post("/studio/api/projects", json={"name": "Evento TR"}).json()["id"]
    client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("voz.wav", b"RIFF" + b"\x00" * 2048, "audio/wav")},
    )
    captured: list[tuple[str, dict]] = []
    broker = client.app.state.broker  # type: ignore[attr-defined]
    original = broker.publish

    def _capture(event_type: str, data: dict) -> None:
        captured.append((event_type, data))
        original(event_type, data)

    broker.publish = _capture  # type: ignore[method-assign]
    try:
        resp = client.post(
            f"/studio/api/projects/{project_id}/transcribe",
            json={"script_segments": _segments()},
        )
    finally:
        broker.publish = original  # type: ignore[method-assign]

    assert resp.status_code == 200, resp.text
    events = [c for c in captured if c[0] == "transcription"]
    assert len(events) == 1
    payload = events[0][1]
    assert payload["project_id"] == project_id
    assert payload["segments_count"] == 2
    assert payload["words_count"] >= 2
    assert payload["simulated"] is True
    # El mismo evento se mapea a un webhook `transcription_done`.
    mapped = map_broker_event("transcription", payload)
    assert mapped is not None and mapped[0] == "transcription_done"


def test_edl_build_rejects_unsupported_aspect_variant(client: TestClient) -> None:
    project_id = client.post("/studio/api/projects", json={"name": "Variantes"}).json()["id"]
    resp = client.post(
        f"/studio/api/projects/{project_id}/edl/build",
        json={
            "aspect_ratios": ["16:9", "21:9"],
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0}],
        },
    )
    assert resp.status_code == 422
    assert "21:9" in resp.text


def test_edl_build_deduplicates_aspect_variants() -> None:
    from comfy_studio.app import EdlBuildRequest

    req = EdlBuildRequest(aspect_ratios=["16:9", "16:9", "9:16"])
    assert req.resolved_aspect_ratios() == ["16:9", "9:16"]
    assert EdlBuildRequest().resolved_aspect_ratios() == []


@pytest.mark.skipif(
    pytest.importorskip("shutil").which("ffmpeg") is None,
    reason="ffmpeg no disponible en este runner",
)
def test_edl_build_renders_each_aspect_variant(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """`aspect_ratios` encola una variante por ratio y publica un evento por variante."""
    import shutil
    import subprocess

    src = tmp_path / "src.mp4"
    proc = subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=#10b981:s=320x180:d=3:r=24",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(src),
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")

    project_id = client.post("/studio/api/projects", json={"name": "Lote"}).json()["id"]
    asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("src.mp4", src.read_bytes(), "video/mp4")},
    ).json()

    captured: list[tuple[str, dict]] = []
    broker = client.app.state.broker  # type: ignore[attr-defined]
    original = broker.publish

    def _capture(event_type: str, data: dict) -> None:
        captured.append((event_type, data))
        original(event_type, data)

    broker.publish = _capture  # type: ignore[method-assign]
    try:
        resp = client.post(
            f"/studio/api/projects/{project_id}/edl/build",
            json={
                "aspect_ratios": ["16:9", "9:16"],
                "source_asset_id": asset["id"],
                "cuts": [{"in_sec": 0.0, "out_sec": 1.5, "reason": "test"}],
            },
        )
    finally:
        broker.publish = original  # type: ignore[method-assign]

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant_count"] == 2
    assert body["aspect_ratios"] == ["16:9", "9:16"]
    dims = {
        v["aspect_ratio"]: (v["verification"]["width"], v["verification"]["height"])
        for v in body["variants"]
    }
    assert dims["16:9"] == (1280, 720)
    assert dims["9:16"] == (720, 1280)
    for variant in body["variants"]:
        assert variant["verification"]["probe_ok"] is True
        assert variant["verification"]["verified"] is True
        assert variant["output_asset"]["id"]
    # Un evento `edl` con status=built por variante ⇒ un `edl_rendered` por webhook.
    built = [d for t, d in captured if t == "edl" and d.get("status") == "built"]
    assert len(built) == 2
    assert {d["aspect_ratio"] for d in built} == {"16:9", "9:16"}
