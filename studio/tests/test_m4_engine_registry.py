"""Tests del registro de motores (M4-SCR-001)."""

from __future__ import annotations

import json

import pytest

from comfy_studio.engines import EngineTier
from comfy_studio.engines.registry import (
    EngineEntry,
    EngineRegistry,
    build_default_registry,
)


def test_default_roster_has_core_engines() -> None:
    reg = build_default_registry()
    names = reg.names()
    for expected in ("comfyui", "blender", "editor", "ffmpeg", "heavy_video", "audio"):
        assert expected in names


def test_comfyui_is_resident_and_wired() -> None:
    reg = build_default_registry()
    comfy = reg.get("comfyui")
    assert comfy is not None
    assert comfy.tier == EngineTier.RESIDENT
    assert comfy.wired is True
    residents = [e.name for e in reg.resident()]
    assert residents == ["comfyui"]


def test_editor_declared_unwired_pending_decision() -> None:
    reg = build_default_registry()
    editor = reg.get("editor")
    assert editor is not None
    assert editor.wired is False
    assert "Remotion" in editor.note


def test_duplicate_registration_rejected() -> None:
    reg = EngineRegistry()
    reg.register(EngineEntry("x", EngineTier.WARM, wired=True))
    with pytest.raises(ValueError, match="duplicado"):
        reg.register(EngineEntry("x", EngineTier.WARM, wired=False))


def test_describe_is_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    reg = build_default_registry()
    desc = reg.describe()
    # Debe serializar a JSON sin objetos vivos.
    blob = json.dumps(desc)
    assert '"dynamic": false' in blob
    assert desc["count"] == 6
    assert all(set(e) == {"name", "tier", "wired", "note"} for e in desc["engines"])
