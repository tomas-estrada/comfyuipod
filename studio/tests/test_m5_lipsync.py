"""Tests del motor de lipsync (M5-SCR, fase 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.lipsync import LipsyncEngine


def _job(tmp_path: Path) -> dict:
    vid = tmp_path / "face.mp4"
    vid.write_bytes(b"\x00")
    aud = tmp_path / "voice.wav"
    aud.write_bytes(b"RIFF")
    return {
        "input_video": str(vid),
        "input_audio": str(aud),
        "output_path": str(tmp_path / "synced.mp4"),
    }


def test_lipsync_is_warm_tier() -> None:
    assert LipsyncEngine().tier == EngineTier.WARM


def test_stub_run_writes_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_LIPSYNC", "1")
    res = LipsyncEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:4] == b"\x00\x00\x00\x18"


def test_missing_inputs_rejected(tmp_path: Path) -> None:
    res = LipsyncEngine().run(EngineRunSpec(job={"output_path": str(tmp_path / "o.mp4")}))
    assert res.ok is False
    assert "inválido" in (res.error or "")


def test_no_model_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_LIPSYNC", raising=False)
    monkeypatch.delenv("STUDIO_LIPSYNC_MODEL_DIR", raising=False)
    res = LipsyncEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "modelo de lipsync" in (res.error or "")


def test_verify_reports_model_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_LIPSYNC", raising=False)
    monkeypatch.delenv("STUDIO_LIPSYNC_MODEL_DIR", raising=False)
    res = LipsyncEngine().verify("any")
    assert res.ok is False
    assert res.payload["model_available"] is False
