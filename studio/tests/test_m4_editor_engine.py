"""Tests del motor editor (M4-SCR-002, F1).

Sin binarios ni GPU: modo stub produce un MP4 real + sidecar, la validación
rechaza timelines inválidas, y un backend no cableado falla de forma tipada.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.editor import EditorEngine, TimelineSpec


def _job(tmp_path: Path, n: int = 3) -> dict:
    return {
        "clips": [
            {
                "path": str(tmp_path / f"scene_{i}.mp4"),
                "duration_s": 3.0,
                "transition_in": "crossfade",
            }
            for i in range(n)
        ],
        "output_path": str(tmp_path / "reel.mp4"),
        "width": 1080,
        "height": 1920,
        "fps": 30,
    }


def test_list_reports_transitions_and_backend() -> None:
    eng = EditorEngine()
    res = eng.list()
    assert res.ok is True
    assert "crossfade" in res.payload["transitions"]
    assert res.payload["backend"] == "unbound"


def test_stub_run_writes_mp4_and_sidecar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_EDITOR", "1")
    eng = EditorEngine(backend="otio_ffmpeg")
    res = eng.run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is True
    assert res.simulated is True
    out = Path(res.payload["output"])
    assert out.exists()
    assert out.read_bytes()[:4] == b"\x00\x00\x00\x18"  # ftyp box
    assert out.with_suffix(".mp4.timeline.json").exists()
    assert res.payload["clips"] == 3
    assert res.payload["total_timeline_s"] == 9.0


def test_remotion_backend_fails_typed_without_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_EDITOR", raising=False)
    monkeypatch.delenv("STUDIO_REMOTION_PROJECT", raising=False)
    # backend real Remotion, pero sin proyecto horneado → falla tipado, no finge.
    eng = EditorEngine(backend="remotion")
    res = eng.run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "Remotion" in (res.stderr_tail or "")


def test_remotion_props_shape() -> None:
    from comfy_studio.engines.editor import Clip, EditorEngine, TimelineSpec

    tl = TimelineSpec(
        clips=[Clip(path=Path("a.mp4"), duration_s=3.0, transition_in="crossfade")],
        output_path=Path("reel.mp4"),
        fps=30,
        width=1080,
        height=1920,
    )
    props = EditorEngine._remotion_props(tl)
    assert props["fps"] == 30 and props["width"] == 1080 and props["height"] == 1920
    assert props["clips"][0]["src"] == "a.mp4"
    assert props["clips"][0]["durationInSeconds"] == 3.0
    assert props["clips"][0]["transitionIn"] == "crossfade"


def test_empty_timeline_rejected(tmp_path: Path) -> None:
    eng = EditorEngine()
    job = {"clips": [], "output_path": str(tmp_path / "x.mp4")}
    res = eng.run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "inválida" in (res.error or "")


def test_bad_transition_rejected(tmp_path: Path) -> None:
    eng = EditorEngine()
    job = _job(tmp_path, 1)
    job["clips"][0]["transition_in"] = "warp-zoom-9000"
    res = eng.run(EngineRunSpec(job=job))
    assert res.ok is False


def test_timeline_total_duration() -> None:
    from comfy_studio.engines.editor import Clip

    tl = TimelineSpec(
        clips=[Clip(path=Path("a.mp4"), duration_s=3.0), Clip(path=Path("b.mp4"), duration_s=5.0)],
        output_path=Path("out.mp4"),
    )
    tl.validate()
    assert tl.total_duration_s() == 8.0
    assert tl.width == 1080 and tl.height == 1920  # vertical por defecto
