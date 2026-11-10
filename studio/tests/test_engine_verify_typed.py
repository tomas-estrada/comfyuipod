"""PR 4: verify tipado distingue instalado-pero-sin-pesos de listo.

Sin red, sin descargar: árbol de archivos sintético en tmp. Los motores con
pesos reportan `reason=weights_missing` y `weights` ausentes cuando falta el
contenido; con contenido presente, `ok=True` y `reason=None`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines.audio import AudioEngine
from comfy_studio.engines.indextts import IndexttsEngine
from comfy_studio.engines.lipsync import LipsyncEngine
from comfy_studio.engines.music_gen import MusicGenEngine
from comfy_studio.engines.roto import RotoEngine
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.engines.voice_clone import VoiceCloneEngine


@pytest.fixture(autouse=True)
def _no_stubs(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "STUDIO_STUB_AUDIO",
        "STUDIO_STUB_INDEXTTS",
        "STUDIO_STUB_VOICE_CLONE",
        "STUDIO_STUB_MUSIC_GEN",
        "STUDIO_STUB_LIPSYNC",
        "STUDIO_STUB_UPSCALER",
        "STUDIO_STUB_ROTO",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _weight_cases(tmp_path: Path):
    return [
        (IndexttsEngine(), "STUDIO_INDEXTTS_MODEL_DIR", ("config.yaml", "gpt.pth"), "x"),
        (VoiceCloneEngine(), "STUDIO_VOICE_CLONE_MODEL_DIR", ("model.bin",), "x"),
        (MusicGenEngine(), "STUDIO_MUSIC_GEN_MODEL_DIR", ("model.bin",), "x"),
        (LipsyncEngine(), "STUDIO_LIPSYNC_MODEL_DIR", ("model.bin",), "x"),
        (UpscalerEngine(), "STUDIO_UPSCALER_MODEL_DIR", ("model.bin",), "x"),
    ]


def test_pesos_ausentes_dir_vacio_es_weights_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for eng, var, _files, sub in _weight_cases(tmp_path):
        empty = tmp_path / eng.name / "empty"
        empty.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(empty))
        res = eng.verify(sub)
        assert res.ok is False, (eng.name, res.error)
        assert res.reason == "weights_missing", eng.name
        assert res.payload.get("weights") == "missing", eng.name
        assert res.error, eng.name


def test_pesos_presentes_es_listo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for eng, var, files, sub in _weight_cases(tmp_path):
        model_dir = tmp_path / eng.name / "model"
        model_dir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            (model_dir / fname).write_bytes(b"fake-weights")
        monkeypatch.setenv(var, str(model_dir))
        res = eng.verify(sub)
        assert res.ok is True, (eng.name, res.error)
        assert res.reason is None, eng.name
        assert res.payload.get("weights") == "present", eng.name


def test_audio_stt_tipado_sin_pesos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path / "data"))
    res = AudioEngine().verify("stt")
    assert res.ok is False
    assert res.reason == "weights_missing"
    assert res.payload.get("weights") == "missing"


def test_audio_stt_tipado_con_pesos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    weights = tmp_path / "data" / "models" / "whisper" / "faster-whisper-large-v3"
    weights.mkdir(parents=True)
    (weights / "model.bin").write_bytes(b"fake")
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path / "data"))
    res = AudioEngine().verify("stt")
    assert res.ok is True, res.error
    assert res.reason is None
    assert res.payload.get("weights") == "present"


def test_roto_pesos_ausentes_tipado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_ROTO_MODEL_DIR", str(tmp_path / "roto-vacio"))
    (tmp_path / "roto-vacio").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(RotoEngine, "_package_available", lambda _self: True)
    res = RotoEngine().verify("x")
    assert res.ok is False
    assert res.reason == "weights_missing"
    assert res.payload.get("weights") is False
    assert res.payload.get("weights_state") == "missing"
