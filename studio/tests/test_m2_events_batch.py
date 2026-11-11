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


@pytest.mark.parametrize(
    ("aspect", "dimensions", "safe_area"),
    [("16:9", (3840, 2160), 5.0), ("9:16", (2160, 3840), 10.0)],
)
def test_editable_package_download_uses_project_owned_edl_source(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    aspect: str,
    dimensions: tuple[int, int],
    safe_area: float,
) -> None:
    """La descarga empaqueta medios del proyecto y conserva el EDL editable."""
    import json
    import zipfile
    from io import BytesIO

    from comfy_studio.engines import EngineResult
    from comfy_studio.engines.editor import EditorEngine
    from comfy_studio.perception import keyframes

    def fake_probe(path, **_kwargs):
        if "studio-edl-remotion-" in str(path):
            return {
                "probe_ok": True,
                "width": dimensions[0],
                "height": dimensions[1],
                "has_video": True,
                "has_audio": True, "duration_sec": 2.0, "fps": 24.0, "color_range": "tv",
                "color_space": "bt709", "color_transfer": "bt709",
                "color_primaries": "bt709",
            }
        if str(path).endswith(".wav"):
            return {"probe_ok": True, "has_video": False, "has_audio": True, "audio_channels": 1}
        return {
            "probe_ok": True, "width": 640, "height": 360, "has_video": True,
            "duration_sec": 4.0,
        }

    def fake_render(_engine, spec):
        assert len(spec.job["audio_paths"]) == 2
        assert (spec.job["width"], spec.job["height"]) == dimensions
        assert spec.job["safe_area_percent"] == safe_area
        output = Path(spec.job["output_path"])
        output.write_bytes(b"\x00\x00\x00\x18ftypmp42synthetic")
        return EngineResult(
            ok=True,
            engine="editor",
            verb="run",
            payload={
                "output": str(output),
                "audio_loudness_lufs": -16.0,
                "true_peak_dbfs": -1.0,
            },
        )

    monkeypatch.setattr(keyframes, "probe_media_info", fake_probe)
    monkeypatch.setattr(EditorEngine, "run", fake_render)
    project_id = client.post("/studio/api/projects", json={"name": "Editable"}).json()["id"]
    uploaded = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("source.mp4", b"\x00\x00\x00\x18ftypmp42synthetic", "video/mp4")},
    )
    assert uploaded.status_code == 201, uploaded.text
    asset_id = uploaded.json()["id"]
    audio = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("music.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")},
    )
    assert audio.status_code == 201, audio.text
    audio_stem = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("stem.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav")},
    )
    assert audio_stem.status_code == 201, audio_stem.text
    edl = client.post(
        f"/studio/api/projects/{project_id}/edl",
        json={
            "title": "Prueba editable",
            "aspect_ratio": aspect,
            "source_asset_id": asset_id,
            "audio_asset_id": audio.json()["id"],
            "audio_asset_ids": [audio.json()["id"], audio_stem.json()["id"]],
            "cuts": [{
                "asset_id": asset_id,
                "in_sec": 1.0,
                "out_sec": 3.0,
                "transition_in": "crossfade",
                "transition_duration_s": 0.4,
            }],
            "titles": [
                {
                    "text": "Título editable",
                    "start_s": 0.0,
                    "end_s": 1.0,
                    "position": "top",
                }
            ],
            "captions": [{"text": "Subtítulo editable", "start_s": 1.0, "end_s": 2.0}],
        },
    )
    assert edl.status_code == 201, edl.text
    response = client.post(
        f"/studio/api/projects/{project_id}/edl/{edl.json()['version']}/editable-package"
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(BytesIO(response.content)) as package:
        project = json.loads(package.read("project.json"))
        manifest = json.loads(package.read("delivery-manifest.json"))
        master_paths = [name for name in package.namelist() if name.startswith("masters/")]
        package.extractall(tmp_path / "delivery")
    assert project["name"] == "Prueba editable"
    assert project["clips"][0]["in_s"] == 1.0
    assert project["clips"][0]["source_path"].startswith("assets/")
    assert project["transitions"] == ["crossfade"]
    assert [title["kind"] for title in project["titles"]] == ["title", "caption"]
    assert project["titles"][0]["position"] == "top"
    assert len(project["audio_stems"]) == 2
    assert all(stem["source_path"].startswith("assets/") for stem in project["audio_stems"])
    assert manifest["captions"] is True
    assert {source["kind"] for source in manifest["sources"]} == {"clip", "audio"}
    assert len(manifest["delivery_artifacts"]) == 1
    assert master_paths == [manifest["delivery_artifacts"][0]["path"]]
    assert manifest["resolution"] == f"{dimensions[0]}x{dimensions[1]}"
    assert manifest["safe_area_percent"] == safe_area
    from comfy_studio.delivery_4k import load_editable_package

    reopened = load_editable_package(tmp_path / "delivery")
    assert reopened.fps == 24 and reopened.color_space == "bt709"
    assert len(reopened.audio_stems) == 2


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
                "fps": 12,
                "color_range": "full",
                "picture_inset_percent": 2.0,
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
        assert variant["verification"]["probed_fps"] == pytest.approx(12.0, abs=0.5)
        assert variant["verification"]["probed_color_range"] == "pc"
        assert variant["verification"]["picture_inset_percent"] == 2.0
        assert variant["output_asset"]["id"]
    # Un evento `edl` con status=built por variante ⇒ un `edl_rendered` por webhook.
    built = [d for t, d in captured if t == "edl" and d.get("status") == "built"]
    assert len(built) == 2
    assert {d["aspect_ratio"] for d in built} == {"16:9", "9:16"}


@pytest.mark.skipif(
    pytest.importorskip("shutil").which("ffmpeg") is None,
    reason="ffmpeg no disponible en este runner",
)
@pytest.mark.parametrize(
    ("aspect_ratio", "expected_dimensions"),
    [("16:9", (3840, 2160)), ("9:16", (2160, 3840))],
)
def test_edl_build_renders_and_probes_exact_4k_canvas(
    client: TestClient,
    tmp_path: Path,
    aspect_ratio: str,
    expected_dimensions: tuple[int, int],
) -> None:
    """La ruta HTTP entrega 4K real y falla si ffprobe no confirma el canvas."""
    import shutil
    import subprocess

    src = tmp_path / "src-4k.mp4"
    proc = subprocess.run(
        [
            shutil.which("ffmpeg"),
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=#10b981:s=320x180:d=1:r=12",
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

    project_id = client.post("/studio/api/projects", json={"name": "4K"}).json()["id"]
    asset = client.post(
        f"/studio/api/projects/{project_id}/assets",
        files={"file": ("src.mp4", src.read_bytes(), "video/mp4")},
    ).json()
    response = client.post(
        f"/studio/api/projects/{project_id}/edl/build",
        json={
            "resolution": "4k",
            "aspect_ratio": aspect_ratio,
            "fps": 12,
            "source_asset_id": asset["id"],
            "cuts": [{"in_sec": 0.0, "out_sec": 1.0, "reason": "4k-smoke"}],
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["resolution"] == "4k"
    assert result["verification"]["probe_ok"] is True
    assert result["verification"]["verified"] is True
    assert (
        result["verification"]["width"],
        result["verification"]["height"],
    ) == expected_dimensions
    assert result["output_asset"]["filename"].endswith("_4k.mp4")
