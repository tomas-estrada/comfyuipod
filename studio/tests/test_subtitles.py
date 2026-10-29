"""Tests de exportación de subtítulos SRT/WebVTT (M2, `M3-IDEA-032`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.subtitles import (
    build_cues,
    format_timecode,
    render_srt,
    render_subtitles,
    render_vtt,
    subtitle_filename,
)


def _segments() -> list[dict]:
    return [
        {
            "id": "seg-001",
            "start": 0.0,
            "end": 2.5,
            "text": "Hola, bienvenidos al estudio.",
            "speaker": "SPEAKER_00",
        },
        {
            "id": "seg-002",
            "start": 2.5,
            "end": 5.25,
            "text": "  Hoy   montamos   un   vídeo.  ",
            "speaker": "SPEAKER_01",
        },
        # Degenerado: se descarta (duraría 0 s).
        {"id": "seg-003", "start": 6.0, "end": 6.0, "text": "vacío"},
        # Sin texto: se descarta.
        {"id": "seg-004", "start": 7.0, "end": 8.0, "text": "   "},
    ]


@pytest.mark.parametrize(
    ("seconds", "style", "expected"),
    [
        (0.0, "srt", "00:00:00,000"),
        (1.5, "srt", "00:00:01,500"),
        (3661.25, "srt", "01:01:01,250"),
        (1.5, "vtt", "00:00:01.500"),
        (-3.0, "vtt", "00:00:00.000"),
    ],
)
def test_timecode_formatting(seconds: float, style: str, expected: str) -> None:
    assert format_timecode(seconds, style=style) == expected  # type: ignore[arg-type]


def test_build_cues_drops_degenerate_and_empty_segments() -> None:
    cues = build_cues(_segments())
    assert len(cues) == 2
    assert [c["index"] for c in cues] == [1, 2]
    assert cues[0]["speaker"] == "SPEAKER_00"
    # El texto se normaliza (espacios colapsados) y conserva el prefijo de hablante.
    assert cues[1]["text"] == "SPEAKER_01: Hoy montamos un vídeo."


def test_build_cues_orders_by_start_time() -> None:
    shuffled = [
        {"start": 5.0, "end": 6.0, "text": "segundo"},
        {"start": 1.0, "end": 2.0, "text": "primero"},
    ]
    cues = build_cues(shuffled)
    assert [c["text"] for c in cues] == ["primero", "segundo"]
    assert [c["index"] for c in cues] == [1, 2]


def test_build_cues_without_speaker_prefix() -> None:
    cues = build_cues(_segments(), include_speakers=False)
    assert cues[0]["text"] == "Hola, bienvenidos al estudio."
    # El hablante sigue disponible como campo, solo no se prefija al texto.
    assert cues[0]["speaker"] == "SPEAKER_00"


def test_render_srt_structure() -> None:
    out = render_srt(_segments())
    lines = out.splitlines()
    assert lines[0] == "1"
    assert lines[1] == "00:00:00,000 --> 00:00:02,500"
    assert lines[2] == "SPEAKER_00: Hola, bienvenidos al estudio."
    assert lines[3] == ""
    assert "00:00:02,500 --> 00:00:05,250" in lines
    # Un único bloque final vacío: SRT válido.
    assert out.endswith("\n")


def test_render_vtt_structure() -> None:
    out = render_vtt(_segments())
    lines = out.splitlines()
    assert lines[0] == "WEBVTT"
    assert lines[1] == ""
    assert "00:00:00.000 --> 00:00:02.500" in lines


def test_render_subtitles_rejects_unknown_format() -> None:
    with pytest.raises(ValueError, match="Formato de subtítulos no soportado"):
        render_subtitles({"segments": _segments()}, fmt="ass")  # type: ignore[arg-type]


def test_subtitle_filename_is_sanitized() -> None:
    assert subtitle_filename({"id": "tr-abc123"}, fmt="srt") == "subtitles-tr-abc123.srt"
    assert subtitle_filename({"id": "../../etc/passwd"}, fmt="vtt") == (
        "subtitles-etcpasswd.vtt"
    )


def _project_with_transcription(client: TestClient) -> tuple[str, str]:
    project_id = client.post("/studio/api/projects", json={"name": "Subtítulos"}).json()["id"]
    store = client.app.state.store  # type: ignore[attr-defined]
    saved = store.save_transcription(
        project_id,
        language="es",
        segments=_segments(),
        diarized=True,
        backend="whisperx",
    )
    return project_id, saved["id"]


def test_subtitles_endpoint_serves_inline_srt(client: TestClient) -> None:
    project_id, tr_id = _project_with_transcription(client)
    resp = client.get(
        f"/studio/api/projects/{project_id}/transcriptions/{tr_id}/subtitles?fmt=srt"
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/x-subrip")
    assert "Content-Disposition" not in resp.headers
    assert "00:00:00,000 --> 00:00:02,500" in resp.text


def test_subtitles_endpoint_serves_vtt_with_download_disposition(client: TestClient) -> None:
    project_id, tr_id = _project_with_transcription(client)
    resp = client.get(
        f"/studio/api/projects/{project_id}/transcriptions/{tr_id}/subtitles"
        "?fmt=vtt&download=true&include_speakers=false"
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/vtt")
    assert f'attachment; filename="subtitles-{tr_id}.vtt"' in resp.headers["content-disposition"]
    assert resp.text.startswith("WEBVTT")
    assert "SPEAKER_00:" not in resp.text


def test_subtitles_endpoint_rejects_invalid_format(client: TestClient) -> None:
    project_id, tr_id = _project_with_transcription(client)
    resp = client.get(
        f"/studio/api/projects/{project_id}/transcriptions/{tr_id}/subtitles?fmt=ass"
    )
    assert resp.status_code == 422


def test_subtitles_endpoint_404_for_unknown_transcription(client: TestClient) -> None:
    project_id, _ = _project_with_transcription(client)
    resp = client.get(
        f"/studio/api/projects/{project_id}/transcriptions/tr-nope/subtitles"
    )
    assert resp.status_code == 404

    missing_project = client.get(
        "/studio/api/projects/no-such-project/transcriptions/tr-x/subtitles"
    )
    assert missing_project.status_code == 404


@pytest.mark.anyio
async def test_export_subtitles_tool_returns_content_and_url(tmp_path: Path) -> None:
    from comfy_studio.agent.tools import ToolContext, create_default_tool_belt
    from comfy_studio.comfyui_client import ComfyUIClient
    from comfy_studio.config import StudioConfig
    from comfy_studio.progress import ProgressBroker
    from comfy_studio.projects import ProjectStore

    store = ProjectStore(tmp_path / "data")
    project = store.create_project(name="Tool subtítulos")
    saved = store.save_transcription(
        project["id"], language="es", segments=_segments(), diarized=True
    )
    cfg = StudioConfig(studio_data_dir=tmp_path / "data", studio_stub_llm=True)
    belt = create_default_tool_belt()
    assert "export_subtitles" in belt.list_names()
    ctx = ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=ComfyUIClient(
            base_url=cfg.studio_comfyui_url,
            store=store,
            broker=ProgressBroker(),
        ),
        broker=ProgressBroker(),
        config=cfg,
    )
    result = await belt.execute("export_subtitles", {"format": "vtt"}, ctx)
    assert result["format"] == "vtt"
    assert result["transcription_id"] == saved["id"]
    assert result["cue_count"] == 2
    assert result["content"].startswith("WEBVTT")
    assert "download=true" in result["download_url"]

    no_project = ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=ctx.comfyui_client,
        broker=ctx.broker,
        config=cfg,
    )
    store2 = ProjectStore(tmp_path / "empty")
    empty = store2.create_project(name="Sin transcripción")
    no_project.project_id = empty["id"]
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError, match="ninguna transcripción"):
        await belt.execute("export_subtitles", {}, no_project)
