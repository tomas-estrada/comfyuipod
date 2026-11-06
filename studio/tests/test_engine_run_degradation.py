"""PR engines-degradacion-honesta: sin pesos/hardware -> limitacion tipada.

Sin GPU ni red: cada run con requisitos ausentes devuelve ok=False con un
mensaje que dice qué falta y cómo remediarlo; nunca un éxito simulado fuera
de stub. Ningún test toca la red ni lanza binarios reales.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.audio import AudioEngine
from comfy_studio.engines.blender import BlenderEngine
from comfy_studio.engines.capture3d import Capture3dEngine
from comfy_studio.engines.compositor import CompositorEngine
from comfy_studio.engines.editor import EditorEngine
from comfy_studio.engines.ffmpeg import FfmpegEngine
from comfy_studio.engines.heavy_video import HeavyVideoEngine
from comfy_studio.engines.indextts import IndexttsEngine
from comfy_studio.engines.lipsync import LipsyncEngine
from comfy_studio.engines.music_gen import MusicGenEngine
from comfy_studio.engines.qa import QaEngine
from comfy_studio.engines.roto import RotoEngine
from comfy_studio.engines.upscaler import UpscalerEngine
from comfy_studio.engines.voice_clone import VoiceCloneEngine


@pytest.fixture(autouse=True)
def _no_stubs(monkeypatch: pytest.MonkeyPatch):
    for var in ("STUDIO_STUB_AUDIO", "STUDIO_STUB_BLENDER", "STUDIO_STUB_EDITOR",
                "STUDIO_STUB_FFMPEG", "STUDIO_STUB_QA", "STUDIO_STUB_ROTO",
                "STUDIO_STUB_UPSCALER", "STUDIO_STUB_INDEXTTS",
                "STUDIO_STUB_VOICE_CLONE", "STUDIO_STUB_MUSIC_GEN",
                "STUDIO_STUB_LIPSYNC", "STUDIO_STUB_CAPTURE3D",
                "STUDIO_STUB_COMPOSITOR", "STUDIO_STUB_HEAVY_VIDEO",
                "STUDIO_DYNAMIC"):
        monkeypatch.delenv(var, raising=False)
    yield


@pytest.fixture()
def _no_bins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """PATH sin binarios reales y sin envs de binarios/modelos."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    for var in ("STUDIO_FFMPEG_BIN", "STUDIO_BLENDER_BIN",
                "STUDIO_CAPTURE3D_BIN_DIR", "STUDIO_COMPOSITOR_BIN",
                "STUDIO_REMOTION_PROJECT", "STUDIO_INDEXTTS_MODEL_DIR",
                "STUDIO_VOICE_CLONE_MODEL_DIR", "STUDIO_VOICE_CLONE_BIN",
                "STUDIO_MUSIC_GEN_MODEL_DIR", "STUDIO_MUSIC_GEN_BIN",
                "STUDIO_LIPSYNC_MODEL_DIR", "STUDIO_LIPSYNC_BIN",
                "STUDIO_UPSCALER_MODEL_DIR", "STUDIO_UPSCALER_BIN",
                "STUDIO_ROTO_MODEL_DIR", "STUDIO_HEAVY_VIDEO_KEY",
                "STUDIO_HEAVY_VIDEO_BASE_URL", "STUDIO_HEAVY_VIDEO_MODEL",
                "COMFY_DATA_DIR"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _job_editor(tmp_path: Path) -> dict:
    return {
        "clips": [{"path": str(tmp_path / "s.mp4"), "duration_s": 2.0,
                   "transition_in": "cut"}],
        "output_path": str(tmp_path / "reel.mp4"),
    }


def test_editor_sin_backend_no_finge(tmp_path: Path) -> None:
    eng = EditorEngine()
    assert eng.backend is None
    res = eng.run(EngineRunSpec(job=_job_editor(tmp_path)))
    assert res.ok is False
    assert "unbound" in (res.error or "") or "STUDIO_STUB_EDITOR" in (res.error or "")
    assert not (tmp_path / "reel.mp4").exists(), "nada de MP4 simulado fuera de stub"
    assert res.simulated is False


def test_pesos_ausentes_fallan_tipado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("STUDIO_INDEXTTS_MODEL_DIR", "STUDIO_VOICE_CLONE_MODEL_DIR",
                "STUDIO_MUSIC_GEN_MODEL_DIR", "STUDIO_LIPSYNC_MODEL_DIR",
                "STUDIO_UPSCALER_MODEL_DIR", "STUDIO_ROTO_MODEL_DIR"):
        monkeypatch.setenv(var, str(tmp_path / "vacio"))
    ref = tmp_path / "ref.wav"
    ref.write_bytes(b"RIFF")
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00")
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    cases = [
        (IndexttsEngine(), {"text": "hola", "output_path": str(tmp_path / "o.wav"),
                            "reference_audio": str(ref)}, "pesos"),
        (VoiceCloneEngine(), {"op": "tts", "text": "hola",
                              "output_path": str(tmp_path / "o.wav"),
                              "reference_audio": str(ref)}, "modelo"),
        (MusicGenEngine(), {"prompt": "lofi", "output_path": str(tmp_path / "o.wav")},
         "modelo"),
        (LipsyncEngine(), {"input_video": str(vid), "input_audio": str(ref),
                           "output_path": str(tmp_path / "o.mp4")}, "modelo"),
        (UpscalerEngine(), {"input_path": str(src), "output_path": str(tmp_path / "o.png"),
                            "scale": 2, "mode": "image"}, "modelo"),
        (RotoEngine(), {"video_path": str(vid), "prompts": ["el robot"],
                        "output_dir": str(tmp_path / "masks")}, "SAM 3"),
    ]
    for eng, job, hint in cases:
        res = eng.run(EngineRunSpec(job=job))
        assert res.ok is False, eng.name
        assert res.simulated is False, eng.name
        assert hint in (res.error or ""), (eng.name, res.error)
        assert res.verb == "run" and res.engine == eng.name


def test_binarios_ausentes_fallan_tipado(
    tmp_path: Path, _no_bins: Path
) -> None:
    vid = tmp_path / "v.mp4"
    vid.write_bytes(b"\x00")
    src = tmp_path / "in.png"
    src.write_bytes(b"\x89PNG\r\n\x1a\n")
    cases = [
        (BlenderEngine(log_dir=tmp_path / "logs"),
         {"mode": "render", "scene_path": str(vid), "output_dir": str(tmp_path / "o")},
         "Blender"),
        (CompositorEngine(),
         {"project_path": str(tmp_path / "p.ntp"), "output_path": str(tmp_path / "o.png")},
         "NatronRenderer"),
        (Capture3dEngine(),
         {"op": "process", "input_path": str(tmp_path), "output_path": str(tmp_path / "o")},
         "Nerfstudio"),
        (QaEngine(), {"video_path": str(vid)}, "FFmpeg"),
        (FfmpegEngine(),
         {"inputs": [str(src)], "output": str(tmp_path / "o.mp4")}, "no disponible"),
    ]
    for eng, job, hint in cases:
        res = eng.run(EngineRunSpec(job=job))
        if asyncio.iscoroutine(res):
            res = asyncio.run(res)
        assert res.ok is False, eng.name
        assert res.simulated is False, eng.name
        assert hint in (res.error or res.stderr_tail or ""), (eng.name, res.error)
        assert res.verb == "run" and res.engine == eng.name


def test_heavy_video_sin_credencial_no_llama(
    tmp_path: Path, _no_bins: Path
) -> None:
    job = {"input_video": str(tmp_path / "v.mp4"),
           "output_path": str(tmp_path / "o.mp4"), "prompt": "x"}
    res = asyncio.run(HeavyVideoEngine().run(EngineRunSpec(job=job)))
    assert res.ok is False
    assert "credencial" in (res.error or "")


def test_audio_stt_sin_pesos_degrada_en_verify_no_en_run(tmp_path: Path) -> None:
    """STT sin faster-whisper: verify ya dice no-listo (PR-4); run no se
    ejecuta aquí porque delegaría al worker real (red/pesos)."""
    res = AudioEngine().verify("stt")
    assert res.ok is False
    assert "faster-whisper" in (res.error or "")
