"""Degradacion honesta del motor blender sin binario (T2).

Sin stub y sin binario (ni `STUDIO_BLENDER_BIN`, ni `PATH`, ni /opt), un `run`
con spec valida debe fallar TIPADO (`ok=False`, `exit_code=127`,
`simulated=False`) y no fingir exito ni marcarlo como simulado.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.blender import BlenderEngine


def _render_job(tmp_path: Path) -> dict:
    scene = tmp_path / "car.glb"
    scene.write_bytes(b"glTF\x02\x00\x00\x00")
    return {
        "mode": "render",
        "scene_path": str(scene),
        "output_dir": str(tmp_path / "out"),
        "engine": "cycles",
        "frames": 4,
    }


def _no_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_BLENDER", raising=False)
    monkeypatch.delenv("STUDIO_BLENDER_BIN", raising=False)
    monkeypatch.setenv("PATH", "")


def test_run_valid_spec_without_binary_fails_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_binary(monkeypatch)
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    assert eng.is_stub() is False
    res = asyncio.run(eng.run(EngineRunSpec(job=_render_job(tmp_path))))
    assert res.ok is False
    assert res.simulated is False
    assert res.exit_code == 127
    assert "Blender no encontrado" in (res.stderr_tail or "")
    assert res.payload["failure_kind"] == "blender_nonzero_exit"


def test_run_without_binary_never_reports_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_binary(monkeypatch)
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = asyncio.run(eng.run(EngineRunSpec(job=_render_job(tmp_path))))
    assert not (res.ok is True and res.simulated is False), "sin binario no puede haber exito real"
    assert res.ok is False
