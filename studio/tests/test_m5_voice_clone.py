"""Tests del motor de clonación de voz / RVC (M5-SCR, fase 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.voice_clone import VoiceCloneEngine


def _clone_job(tmp_path: Path) -> dict:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    src = tmp_path / "src.wav"
    src.write_bytes(b"RIFF")
    return {
        "op": "clone",
        "reference_audio": str(ref),
        "source_audio": str(src),
        "output_path": str(tmp_path / "out.wav"),
    }


def test_voice_clone_is_warm_tier() -> None:
    assert VoiceCloneEngine().tier == EngineTier.WARM


def test_list_reports_ops() -> None:
    res = VoiceCloneEngine().list()
    assert res.ok is True
    assert set(res.payload["ops"]) == {"clone", "tts"}


def test_stub_clone_writes_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_VOICE_CLONE", "1")
    res = VoiceCloneEngine().run(EngineRunSpec(job=_clone_job(tmp_path)))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:4] == b"RIFF"


def test_stub_tts_writes_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_VOICE_CLONE", "1")
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    job = {
        "op": "tts",
        "reference_audio": str(ref),
        "text": "hola con tu voz",
        "output_path": str(tmp_path / "v.wav"),
    }
    res = VoiceCloneEngine().run(EngineRunSpec(job=job))
    assert res.ok is True and res.simulated is True


def test_clone_missing_source_rejected(tmp_path: Path) -> None:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    job = {"op": "clone", "reference_audio": str(ref), "output_path": str(tmp_path / "o.wav")}
    res = VoiceCloneEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "source_audio" in (res.error or "")


def test_bad_op_rejected(tmp_path: Path) -> None:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    job = {"op": "sing-opera", "reference_audio": str(ref), "output_path": str(tmp_path / "o.wav")}
    res = VoiceCloneEngine().run(EngineRunSpec(job=job))
    assert res.ok is False


def test_no_model_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_VOICE_CLONE", raising=False)
    monkeypatch.delenv("STUDIO_VOICE_CLONE_MODEL_DIR", raising=False)
    res = VoiceCloneEngine().run(EngineRunSpec(job=_clone_job(tmp_path)))
    assert res.ok is False
    assert "modelo RVC" in (res.error or "")


def test_verify_reports_model_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_VOICE_CLONE", raising=False)
    monkeypatch.delenv("STUDIO_VOICE_CLONE_MODEL_DIR", raising=False)
    res = VoiceCloneEngine().verify("any")
    assert res.ok is False
    assert res.payload["model_available"] is False
