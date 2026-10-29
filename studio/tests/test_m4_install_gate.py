"""Tests de la puerta de decisión de instalación de motor (M4-SCR-003, F6)."""

from __future__ import annotations

import pytest

from comfy_studio.engines.install_gate import (
    InstallRequest,
    PromotionLedger,
    decide_install,
)
from comfy_studio.engines.registry import build_default_registry


def _req(**kw: object) -> InstallRequest:
    base = {
        "engine_name": "nerfstudio",
        "warm_attempted": True,
        "task_value": 10.0,
        "est_idle_cost": 2.0,
        "sandbox_safe": True,
        "source": "github.com/nerfstudio",
    }
    base.update(kw)
    return InstallRequest(**base)  # type: ignore[arg-type]


def test_refused_when_dynamic_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    d = decide_install(_req(), build_default_registry())
    assert d.allowed is False
    assert "determinista" in d.reasons[0]


def test_refused_when_already_in_roster(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(engine_name="comfyui"), build_default_registry())
    assert d.allowed is False
    assert "ya está en el roster" in d.reasons[0]


def test_refused_when_warm_not_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(warm_attempted=False), build_default_registry())
    assert d.allowed is False
    assert any("caliente no se ha intentado" in r for r in d.reasons)


def test_refused_when_cost_exceeds_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(task_value=2.0, est_idle_cost=5.0), build_default_registry())
    assert d.allowed is False
    assert any("no supera" in r for r in d.reasons)


def test_refused_when_not_sandbox_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(sandbox_safe=False), build_default_registry())
    assert d.allowed is False
    assert any("sandbox" in r for r in d.reasons)


def test_allowed_and_promote_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(task_value=10.0, est_idle_cost=2.0), build_default_registry())
    assert d.allowed is True
    assert d.promote_candidate is True  # 10 >= 2 * 2
    assert any("sandbox" in r for r in d.reasons)


def test_allowed_but_not_promote_when_margin_thin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    d = decide_install(_req(task_value=3.0, est_idle_cost=2.0), build_default_registry())
    assert d.allowed is True
    assert d.promote_candidate is False  # 3 < 2 * 2


def test_promotion_ledger_threshold() -> None:
    ledger = PromotionLedger(promote_threshold=3)
    for _ in range(3):
        ledger.record_use("nerfstudio", source="github.com/nerfstudio")
    ledger.record_use("natron")
    assert ledger.ready_to_promote() == ["nerfstudio"]
