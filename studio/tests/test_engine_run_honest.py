"""PR 5: run honesto con limitaciones tipadas, sin éxito simulado.

Fuera de stub, pedir `run` a un motor sin pesos devuelve ok=False,
simulated=False y reason="weights_missing". En stub, `simulated=True` solo si
se pidió explícitamente.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.indextts import IndexttsEngine
from comfy_studio.engines.lipsync import LipsyncEngine
from comfy_studio.engines.music_gen import MusicGenEngine
from comfy_studio.engines.roto import RotoEngine
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.engines.voice_clone import VoiceCloneEngine


@pytest.fixture(autouse=True)
def _no_stubs(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "STUDIO_STUB_INDEXTTS",
        "STUDIO_STUB_VOICE_CLONE",
        "STUDIO_STUB_MUSIC_GEN",
        "STUDIO_STUB_LIPSYNC",
        "STUDIO_STUB_UPSCALER",
        "STUDIO_STUB_ROTO",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _empty_model_dir(tmp_path: Path, env_var: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / env_var.lower()
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(env_var, str(d))
    return d


def test_run_sin_pesos_devuelve_limitacion_tipada(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    out = tmp_path / "out"

    cases = [
        (IndexttsEngine(), "STUDIO_INDEXTTS_MODEL_DIR", {
            "text": "hola", "output_path": str(out / "o.wav"), "reference_audio": str(ref),
        }),
        (VoiceCloneEngine(), "STUDIO_VOICE_CLONE_MODEL_DIR", {
            "op": "tts", "text": "hola", "output_path": str(out / "o.wav"),
            "reference_audio": str(ref),
        }),
        (MusicGenEngine(), "STUDIO_MUSIC_GEN_MODEL_DIR", {
            "prompt": "lofi", "output_path": str(out / "o.wav"),
        }),
        (LipsyncEngine(), "STUDIO_LIPSYNC_MODEL_DIR", {
            "input_video": str(src), "input_audio": str(ref),
            "output_path": str(out / "o.mp4"),
        }),
        (UpscalerEngine(), "STUDIO_UPSCALER_MODEL_DIR", {
            "input_path": str(src), "output_path": str(out / "o.png"),
            "scale": 2, "mode": "image",
        }),
        (RotoEngine(), "STUDIO_ROTO_MODEL_DIR", {
            "video_path": str(src), "prompts": ["el robot"],
            "output_dir": str(out / "masks"),
        }),
    ]

    for eng, var, job in cases:
        _empty_model_dir(tmp_path / eng.name, var, monkeypatch)
        if eng.name == "roto":
            monkeypatch.setattr(RotoEngine, "_package_available", lambda _self: True)
        res = eng.run(EngineRunSpec(job=job))
        assert res.ok is False, (eng.name, res.error)
        assert res.simulated is False, eng.name
        assert res.reason == "weights_missing", (eng.name, res.reason, res.error)
        assert res.error, eng.name


def test_stub_es_simulado_solo_cuando_se_pide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 16)
    monkeypatch.setenv("STUDIO_STUB_INDEXTTS", "1")
    eng = IndexttsEngine()
    res = eng.run(EngineRunSpec(job={
        "text": "hola",
        "output_path": str(tmp_path / "out.wav"),
        "reference_audio": str(ref),
    }))
    assert res.ok is True
    assert res.simulated is True
