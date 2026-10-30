"""Tests del motor de audio (M4-SCR-002, F5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.audio import AudioEngine


def test_audio_is_warm_tier() -> None:
    assert AudioEngine().tier == EngineTier.WARM


def test_list_reports_ops_and_wired() -> None:
    res = AudioEngine().list()
    assert res.ok is True
    assert set(res.payload["ops"]) == {"tts", "stt", "music", "lipsync"}
    assert set(res.payload["wired_ops"]) == {"stt", "tts"}


def test_stub_tts_writes_wav(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_AUDIO", "1")
    job = {"op": "tts", "text": "hola mundo", "output_path": str(tmp_path / "v.wav")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is True and res.simulated is True
    assert Path(res.payload["output"]).read_bytes()[:4] == b"RIFF"


def test_stub_stt_writes_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_AUDIO", "1")
    src = tmp_path / "in.wav"
    src.write_bytes(b"RIFF")
    job = {"op": "stt", "input_audio": str(src), "output_path": str(tmp_path / "t.txt")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is True
    assert "transcripción" in Path(res.payload["output"]).read_text(encoding="utf-8")


def test_stub_lipsync_writes_mp4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_AUDIO", "1")
    vid = tmp_path / "scene.mp4"
    vid.write_bytes(b"\x00")
    aud = tmp_path / "voice.wav"
    aud.write_bytes(b"RIFF")
    job = {
        "op": "lipsync",
        "input_video": str(vid),
        "input_audio": str(aud),
        "output_path": str(tmp_path / "synced.mp4"),
    }
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is True
    assert Path(res.payload["output"]).read_bytes()[:4] == b"\x00\x00\x00\x18"


def test_tts_without_text_rejected(tmp_path: Path) -> None:
    job = {"op": "tts", "output_path": str(tmp_path / "v.wav")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "text" in (res.error or "")


def test_lipsync_missing_inputs_rejected(tmp_path: Path) -> None:
    job = {"op": "lipsync", "output_path": str(tmp_path / "x.mp4")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is False


def test_unwired_music_fails_typed_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_STUB_AUDIO", raising=False)
    job = {"op": "music", "prompt": "lofi beat", "output_path": str(tmp_path / "m.wav")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "F6" in (res.error or "")


def test_bad_op_rejected(tmp_path: Path) -> None:
    job = {"op": "teleport", "output_path": str(tmp_path / "x")}
    res = AudioEngine().run(EngineRunSpec(job=job))
    assert res.ok is False
    assert "op" in (res.error or "")



def test_real_stt_delegates_to_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Fuera de stub, STT delega en el worker ASR; escribimos la transcripción.
    monkeypatch.delenv("STUDIO_STUB_AUDIO", raising=False)
    import comfy_studio.ml.runner as runner
    from comfy_studio.ml import MlWorkerResponse

    def _fake_run_worker(subcommand, payload=None, **kw):
        assert subcommand == "asr_transcribe"
        assert payload and "audio_path" in payload
        return MlWorkerResponse(
            ok=True,
            subcommand="asr_transcribe",
            payload={"text": "hola desde el worker", "language": "es", "simulated": False},
        )

    monkeypatch.setattr(runner, "run_worker", _fake_run_worker)
    src = tmp_path / "in.wav"
    src.write_bytes(b"RIFF")
    out = tmp_path / "t.txt"
    res = AudioEngine().run(
        EngineRunSpec(job={"op": "stt", "input_audio": str(src), "output_path": str(out)})
    )
    assert res.ok is True
    assert out.read_text(encoding="utf-8") == "hola desde el worker"
    assert res.payload["language"] == "es"


def test_real_tts_typed_pending_outside_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # TTS usa Kokoro; ausente en CI -> falla TIPADO mencionando Kokoro, no finge.
    monkeypatch.delenv("STUDIO_STUB_AUDIO", raising=False)
    res = AudioEngine().run(
        EngineRunSpec(job={"op": "tts", "text": "hola", "output_path": str(tmp_path / "v.wav")})
    )
    assert res.ok is False
    assert "Kokoro" in (res.error or "")
