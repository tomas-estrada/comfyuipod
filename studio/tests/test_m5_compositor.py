"""Tests del motor de composición / Natron (M5-SCR, fase 10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.compositor import CompositorEngine


def _job(tmp_path: Path) -> dict:
    proj = tmp_path / "comp.ntp"
    proj.write_text("natron project", encoding="utf-8")
    return {
        "project_path": str(proj),
        "output_path": str(tmp_path / "frame.png"),
        "frame_start": 1,
        "frame_end": 10,
    }


def test_compositor_is_warm_tier() -> None:
    assert CompositorEngine().tier == EngineTier.WARM


def test_stub_run_writes_png_and_counts_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_STUB_COMPOSITOR", "1")
    res = CompositorEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert res.payload["frames"] == 10


def test_bad_frame_range_rejected(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job["frame_start"] = 10
    job["frame_end"] = 1
    res = CompositorEngine().run(EngineRunSpec(job=job))
    assert res.ok is False


def test_no_binary_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_COMPOSITOR", raising=False)
    monkeypatch.setenv("STUDIO_COMPOSITOR_BIN", "")
    monkeypatch.setenv("PATH", "")
    res = CompositorEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "NatronRenderer no disponible" in (res.error or "")


def test_verify_reports_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_COMPOSITOR", raising=False)
    monkeypatch.setenv("STUDIO_COMPOSITOR_BIN", "")
    monkeypatch.setenv("PATH", "")
    res = CompositorEngine().verify("any")
    assert res.ok is False
    assert res.payload["available"] is False
