"""Tests del motor indextts (IndexTTS-2.5 zero-shot) y su integración con generate_speech."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.indextts import IndexttsEngine


def _spec(tmp_path: Path, **over: object) -> dict:
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16)
    job: dict = {
        "text": "¡Súbete al coche, vamos!",
        "output_path": str(tmp_path / "out.wav"),
        "reference_audio": str(ref),
        "lang": "es",
    }
    job.update(over)
    return job


def test_indextts_stub_run_writes_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_INDEXTTS", "1")
    engine = IndexttsEngine()
    job = _spec(tmp_path)
    result = engine.run(EngineRunSpec(job=job))
    assert result.ok is True
    assert result.simulated is True
    assert Path(job["output_path"]).exists()


def test_indextts_rejects_invalid_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_INDEXTTS", "1")
    engine = IndexttsEngine()
    # texto vacío
    r = engine.run(EngineRunSpec(job=_spec(tmp_path, text="  ")))
    assert r.ok is False
    assert "text" in (r.error or "")
    # referencia inexistente
    r2 = engine.run(
        EngineRunSpec(job=_spec(tmp_path, reference_audio=str(tmp_path / "no.wav")))
    )
    assert r2.ok is False
    # lang no soportado
    r3 = engine.run(EngineRunSpec(job=_spec(tmp_path, lang="xx")))
    assert r3.ok is False


def test_indextts_fails_typed_without_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_INDEXTTS", raising=False)
    monkeypatch.setenv("STUDIO_INDEXTTS_MODEL_DIR", str(tmp_path / "empty"))
    engine = IndexttsEngine()
    result = engine.run(EngineRunSpec(job=_spec(tmp_path)))
    assert result.ok is False
    assert "pesos" in (result.error or "")
    assert "STUDIO_STUB_INDEXTTS=1" in (result.error or "")


def test_indextts_verify_and_list_report_weights_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_INDEXTTS", raising=False)
    monkeypatch.setenv("STUDIO_INDEXTTS_MODEL_DIR", str(tmp_path / "empty"))
    engine = IndexttsEngine()
    v = engine.verify("weights")
    assert v.ok is False
    monkeypatch.setenv("STUDIO_STUB_INDEXTTS", "1")
    assert engine.verify("weights").ok is True


def test_indextts_registered_in_roster() -> None:
    from comfy_studio.engines.registry import build_default_registry

    desc = build_default_registry().describe()
    names = {e["name"] for e in desc["engines"]}
    assert "indextts" in names
    entry = next(e for e in desc["engines"] if e["name"] == "indextts")
    assert entry["wired"] is True
