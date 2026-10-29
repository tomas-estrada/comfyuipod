"""Tests del adaptador Blender al contrato de motores (M4-SCR-002).

Modo stub (STUDIO_STUB_BLENDER=1): sin binario de Blender ni GPU.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
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


def test_blender_is_warm_tier(tmp_path: Path) -> None:
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    assert eng.tier == EngineTier.WARM


def test_list_reports_modes_and_formats(tmp_path: Path) -> None:
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = eng.list()
    assert res.ok is True
    assert set(res.payload["modes"]) == {"render", "export"}
    assert "usd" in res.payload["export_formats"]


def test_stub_render_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = asyncio.run(eng.run(EngineRunSpec(job=_render_job(tmp_path))))
    assert res.ok is True
    assert res.simulated is True
    assert res.payload["failure_kind"] == "ok"
    assert len(res.payload["outputs"]) >= 1


def test_stub_export_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    scene = tmp_path / "car.glb"
    scene.write_bytes(b"glTF\x02\x00\x00\x00")
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    job = {
        "mode": "export",
        "scene_path": str(scene),
        "output_dir": str(tmp_path / "exp"),
        "formats": ["usd", "gltf"],
    }
    res = asyncio.run(eng.run(EngineRunSpec(job=job)))
    assert res.ok is True
    assert res.simulated is True


def test_bad_mode_rejected(tmp_path: Path) -> None:
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = asyncio.run(
        eng.run(EngineRunSpec(job={"mode": "teleport", "scene_path": "x", "output_dir": "y"}))
    )
    assert res.ok is False
    assert "mode" in (res.error or "")


def test_install_blocked_when_dynamic_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    eng = BlenderEngine(log_dir=tmp_path / "logs")
    res = eng.install("some-addon")
    assert res.ok is False
    assert "STUDIO_DYNAMIC=1" in (res.error or "")
