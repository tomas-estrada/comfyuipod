"""Degradacion honesta del motor ffmpeg sin binario (T2).

Sin stub y sin binario (ni `STUDIO_FFMPEG_BIN` ni `PATH`), un `run` con spec
valida debe fallar TIPADO (`ok=False`, remedacion clara) y no fingir exito:
ni `ok=True`, ni `simulated=True`, ni artefacto de salida a medias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.ffmpeg import FfmpegEngine


def _valid_job(tmp_path: Path) -> dict:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00" * 64)
    return {"inputs": [str(src)], "output": str(tmp_path / "out.mp4")}


def _no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_FFMPEG", raising=False)
    monkeypatch.delenv("STUDIO_FFMPEG_BIN", raising=False)
    monkeypatch.setenv("PATH", "")


def test_run_valid_spec_without_binary_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_binary(monkeypatch)
    eng = FfmpegEngine()
    assert eng.is_stub() is False
    res = eng.run(EngineRunSpec(job=_valid_job(tmp_path)))
    assert res.ok is False
    assert "FFmpeg no disponible" in (res.error or "")
    assert res.simulated is False
    assert not (tmp_path / "out.mp4").exists()


def test_run_without_binary_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_binary(monkeypatch)
    res = FfmpegEngine().run(EngineRunSpec(job=_valid_job(tmp_path)))
    assert not (res.ok is True and res.simulated is False), "sin binario no puede haber exito real"
    assert res.ok is False
