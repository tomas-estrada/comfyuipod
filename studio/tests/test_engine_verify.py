"""PR engines-verify: instalado-pero-sin-pesos no es listo, con mensajes tipados."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from comfy_studio.engines.audio import AudioEngine, stt_weights_available
from comfy_studio.engines.blender import BlenderEngine
from comfy_studio.engines.indextts import IndexttsEngine
from comfy_studio.engines.lipsync import LipsyncEngine
from comfy_studio.engines.music_gen import MusicGenEngine
from comfy_studio.engines.roto import RotoEngine
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.engines.voice_clone import VoiceCloneEngine


@pytest.fixture(autouse=True)
def _no_stubs(monkeypatch: pytest.MonkeyPatch):
    for var in ("STUDIO_STUB_AUDIO", "STUDIO_STUB_BLENDER", "STUDIO_STUB_INDEXTTS",
                "STUDIO_STUB_VOICE_CLONE", "STUDIO_STUB_MUSIC_GEN",
                "STUDIO_STUB_LIPSYNC", "STUDIO_STUB_UPSCALER", "STUDIO_STUB_ROTO"):
        monkeypatch.delenv(var, raising=False)
    yield


def _writable_exe(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nprintf 'Blender 5.2.4\\n'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# ---------------------------------------------------------------------------
# audio STT: faster-whisper en disco manda
# ---------------------------------------------------------------------------


def test_stt_sin_pesos_no_esta_listo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path / "nada"))
    assert stt_weights_available() is False
    res = AudioEngine().verify("stt")
    assert res.ok is False
    assert "faster-whisper" in (res.error or "")
    assert res.payload["weights_ok"] is False


def test_stt_con_pesos_listo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weights = tmp_path / "models" / "whisper" / "faster-whisper-large-v3"
    weights.mkdir(parents=True)
    (weights / "model.bin").write_bytes(b"fake")
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path))
    assert stt_weights_available() is True
    res = AudioEngine().verify("stt")
    assert res.ok is True, res.error
    assert res.payload["weights_ok"] is True


def test_otras_ops_conservan_semantica(monkeypatch: pytest.MonkeyPatch) -> None:
    assert AudioEngine().verify("tts").ok is True
    bad = AudioEngine().verify("music")
    assert bad.ok is False and "F6" in (bad.error or "")


# ---------------------------------------------------------------------------
# blender: sin binario no hay nada que verificar
# ---------------------------------------------------------------------------


def test_blender_sin_binario_no_listo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_BLENDER_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "vacio"))
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = eng.verify("render")
    assert res.ok is False
    assert "ausente" in (res.error or "")
    assert res.payload["binary"] in (None, "")


def test_blender_con_binario_listo(tmp_path: Path) -> None:
    exe = _writable_exe(tmp_path / "bin" / "blender-fake")
    eng = BlenderEngine(log_dir=tmp_path / "logs", blender_bin=exe)
    res = eng.verify("render")
    assert res.ok is True, res.error
    assert res.payload["binary"] == exe


# ---------------------------------------------------------------------------
# barrido: motores con pesos declaran ausencia con mensaje tipado
# ---------------------------------------------------------------------------


def _engines_con_pesos() -> list:
    return [
        (IndexttsEngine(), "STUDIO_INDEXTTS_MODEL_DIR", ("config.yaml", "gpt.pth")),
        (VoiceCloneEngine(), "STUDIO_VOICE_CLONE_MODEL_DIR", ("model.bin",)),
        (MusicGenEngine(), "STUDIO_MUSIC_GEN_MODEL_DIR", ("model.bin",)),
        (LipsyncEngine(), "STUDIO_LIPSYNC_MODEL_DIR", ("model.bin",)),
        (UpscalerEngine(), "STUDIO_UPSCALER_MODEL_DIR", ("model.bin",)),
        (RotoEngine(), "STUDIO_ROTO_MODEL_DIR", ("model.bin",)),
    ]


def test_pesos_ausentes_no_listos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for eng, var, _files in _engines_con_pesos():
        monkeypatch.setenv(var, str(tmp_path / "vacio" / eng.name))
        res = eng.verify("x")
        assert res.ok is False, eng.name
        assert res.verb == "verify" and res.engine == eng.name
        assert res.error, eng.name
        assert any(k in res.error for k in ("ausente", "pesos", "modelo", "weights")), eng.name


def test_pesos_presentes_listos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for eng, var, files in _engines_con_pesos():
        if eng.name == "roto":
            continue  # paquete sam3 ausente aquí: se prueba aparte
        model_dir = tmp_path / "models" / eng.name
        model_dir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            (model_dir / fname).write_bytes(b"fake-weights")
        monkeypatch.setenv(var, str(model_dir))
        res = eng.verify("x")
        assert res.ok is True, (eng.name, res.error)


def test_roto_exige_paquete_y_pesos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    model_dir = tmp_path / "sam3"
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "model.bin").write_bytes(b"fake")
    monkeypatch.setenv("STUDIO_ROTO_MODEL_DIR", str(model_dir))
    res = RotoEngine().verify("x")
    assert res.payload["weights"] is True
    assert res.ok is False  # falta el paquete sam3: instalado no es listo
