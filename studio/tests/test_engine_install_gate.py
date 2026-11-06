"""PR engines-install-gate: la puerta solo DECIDE; con dinámico off nada instala."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from comfy_studio.engines.install_gate import (
    InstallDecision,
    InstallRequest,
    PromotionLedger,
    decide_install,
)
from comfy_studio.engines.registry import build_default_registry, resolve_engine_class


def _req(**kw) -> InstallRequest:
    base = {
        "engine_name": "motor_nuevo",
        "warm_attempted": True,
        "task_value": 10.0,
        "est_idle_cost": 2.0,
        "sandbox_safe": True,
    }
    base.update(kw)
    return InstallRequest(**base)  # type: ignore[arg-type]


def _explotar(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*a, **k):  # noqa: ANN001, ANN002
        raise AssertionError("decide_install no debe ejecutar nada")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def _decisiones_todas() -> list[InstallDecision]:
    reg = build_default_registry()
    return [
        decide_install(_req(), reg),
        decide_install(_req(warm_attempted=False), reg),
        decide_install(_req(task_value=1.0, est_idle_cost=9.0), reg),
        decide_install(_req(sandbox_safe=False), reg),
        decide_install(_req(engine_name="comfyui"), reg),
    ]


def test_decidir_no_ejecuta_nada(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ni siquiera con dinámico on la puerta toca subprocesos/red/disco."""
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    _explotar(monkeypatch)
    for d in _decisiones_todas():
        assert isinstance(d, InstallDecision)
        assert isinstance(d.allowed, bool) and d.reasons
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    _explotar(monkeypatch)
    d = decide_install(_req(), build_default_registry())
    assert d.allowed is False


def test_fronteras_exacto_igual_y_doble(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    reg = build_default_registry()
    igual = decide_install(_req(task_value=2.0, est_idle_cost=2.0), reg)
    assert igual.allowed is False, "valor == coste no compensa la GPU ociosa"
    doble = decide_install(_req(task_value=4.0, est_idle_cost=2.0), reg)
    assert doble.allowed is True and doble.promote_candidate is True
    justo = decide_install(_req(task_value=3.99, est_idle_cost=2.0), reg)
    assert justo.allowed is True and justo.promote_candidate is False


def test_decision_serializable() -> None:
    d = decide_install(_req(), build_default_registry())
    assert json.loads(json.dumps(d.to_dict()))["allowed"] is False
    assert set(d.to_dict()) == {"allowed", "reasons", "promote_candidate"}


def test_ledger_umbral_y_serie() -> None:
    ledger = PromotionLedger(promote_threshold=2)
    rec = ledger.record_use("m1", source="git")
    assert rec.times_used == 1 and rec.source == "git"
    assert ledger.ready_to_promote() == []
    ledger.record_use("m1")
    ledger.record_use("m0")
    assert ledger.ready_to_promote() == ["m1"]
    pub = ledger.to_dict()
    assert pub["threshold"] == 2
    assert [r["engine_name"] for r in pub["records"]] == ["m0", "m1"]
    assert json.loads(json.dumps(pub))


def _construir(name: str, tmp_path: Path):
    cls = resolve_engine_class(name)
    if name == "blender":
        return cls(log_dir=tmp_path)
    return cls()


def test_install_con_dinamico_apagado_no_hace_nada(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Con STUDIO_DYNAMIC off, ningún `install` instala: ok=False tipado,
    sin subprocesos y sin escribir en el cwd."""
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    monkeypatch.delenv("STUDIO_STUB_FFMPEG", raising=False)
    _explotar(monkeypatch)
    monkeypatch.chdir(tmp_path)
    reg = build_default_registry()
    probados = 0
    for name in reg.names():
        if name == "comfyui":
            continue  # exige cliente real; cubierto en test_m4_comfyui_engine
        eng = _construir(name, tmp_path)
        res = eng.install("submotor-cualquiera")
        assert res.ok is False, name
        assert res.verb == "install" and res.engine == name
        probados += 1
    assert probados == 19
    assert list(tmp_path.iterdir()) == [], "install no debe escribir nada"
