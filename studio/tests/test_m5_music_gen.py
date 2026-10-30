"""Tests del motor de generación de música (M5-SCR, fase 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.music_gen import MusicGenEngine


def _job(tmp_path: Path) -> dict:
    return {
        "prompt": "lofi beat, warm, 90bpm",
        "duration_s": 15.0,
        "output_path": str(tmp_path / "music.wav"),
    }


def test_music_gen_is_warm_tier() -> None:
    assert MusicGenEngine().tier == EngineTier.WARM


def test_stub_run_writes_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_MUSIC_GEN", "1")
    res = MusicGenEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:4] == b"RIFF"


def test_empty_prompt_rejected(tmp_path: Path) -> None:
    job = {"prompt": "  ", "output_path": str(tmp_path / "m.wav")}
    res = MusicGenEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "prompt" in (res.error or "")


def test_bad_duration_rejected(tmp_path: Path) -> None:
    job = _job(tmp_path)
    job["duration_s"] = 9999.0
    res = MusicGenEngine().run(EngineRunSpec(job=job))
    assert res.ok is False


def test_no_model_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_MUSIC_GEN", raising=False)
    monkeypatch.delenv("STUDIO_MUSIC_GEN_MODEL_DIR", raising=False)
    res = MusicGenEngine().run(EngineRunSpec(job=_job(tmp_path)))
    assert res.ok is False
    assert "modelo de música" in (res.error or "")


def test_verify_reports_model_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_MUSIC_GEN", raising=False)
    monkeypatch.delenv("STUDIO_MUSIC_GEN_MODEL_DIR", raising=False)
    res = MusicGenEngine().verify("any")
    assert res.ok is False
    assert res.payload["model_available"] is False
