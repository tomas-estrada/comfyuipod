"""Tests de la sonda de salud de motores (M4-SCR, F7 seed)."""

from __future__ import annotations

import json

import pytest

from comfy_studio.engines.health import (
    EngineNotReadyError,
    assert_production_ready,
    health_report,
    probe_engines,
)
from comfy_studio.engines.registry import build_default_registry


def test_comfyui_ready_editor_unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_COMFYUI", raising=False)
    reg = build_default_registry()
    states = {h.name: h.state for h in probe_engines(reg)}
    assert states["comfyui"] == "ready"  # wired, no stub
    assert states["editor"] == "unwired"  # declarado, sin backend
    assert states["heavy_video"] == "unwired"


def test_stub_mode_reports_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    reg = build_default_registry()
    states = {h.name: h.state for h in probe_engines(reg)}
    assert states["blender"] == "stub"


def test_health_report_serializable_with_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_BLENDER", raising=False)
    monkeypatch.delenv("STUDIO_STUB_COMFYUI", raising=False)
    reg = build_default_registry()
    rep = health_report(reg)
    json.dumps(rep)  # debe serializar sin objetos vivos
    assert rep["counts"]["ready"] >= 1
    assert rep["counts"]["unwired"] >= 1
    assert sum(rep["counts"].values()) == len(rep["engines"])


def test_assert_passes_for_ready_engine() -> None:
    reg = build_default_registry()
    # comfyui está cableado → no lanza.
    assert_production_ready(reg, required=["comfyui"])


def test_assert_fails_for_unwired_engine() -> None:
    reg = build_default_registry()
    with pytest.raises(EngineNotReadyError, match="editor"):
        assert_production_ready(reg, required=["editor"])


def test_assert_fails_for_stub_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_COMFYUI", "1")
    reg = build_default_registry()
    with pytest.raises(EngineNotReadyError, match="stub"):
        assert_production_ready(reg, required=["comfyui"])


def test_assert_fails_for_missing_engine() -> None:
    reg = build_default_registry()
    with pytest.raises(EngineNotReadyError, match="roster"):
        assert_production_ready(reg, required=["nonexistent_engine"])
