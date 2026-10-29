"""Tests del contrato de motores (M4-SCR-001, F0).

Prueban la interfaz uniforme de 4 verbos sin binarios ni GPU: tipos de
respuesta, verbos válidos, modo stub, interruptor STUDIO_DYNAMIC y la guarda de
install en pod determinista.
"""

from __future__ import annotations

import pytest

from comfy_studio.engines import (
    ENGINE_DEFAULT_TIMEOUTS,
    ENGINE_VERBS,
    EngineResult,
    EngineRunSpec,
    EngineTier,
    is_dynamic_enabled,
    is_stub_mode,
    stub_env_name,
)
from comfy_studio.engines.base import Engine


def test_engine_verbs_are_the_four() -> None:
    assert ENGINE_VERBS == {"list", "install", "verify", "run"}
    for verb in ENGINE_VERBS:
        assert verb in ENGINE_DEFAULT_TIMEOUTS
        assert ENGINE_DEFAULT_TIMEOUTS[verb] > 0


def test_engine_result_public_dict_roundtrip() -> None:
    res = EngineResult(ok=True, engine="demo", verb="run", payload={"out": "x.mp4"})
    pub = res.to_public_dict()
    assert pub["ok"] is True
    assert pub["engine"] == "demo"
    assert pub["verb"] == "run"
    assert pub["payload"] == {"out": "x.mp4"}
    assert pub["simulated"] is False
    # Nunca expone objetos internos: solo claves planas serializables.
    assert set(pub) == {
        "ok",
        "engine",
        "verb",
        "payload",
        "error",
        "exit_code",
        "stderr_tail",
        "simulated",
    }


def test_run_spec_defaults() -> None:
    spec = EngineRunSpec(job={"template": "wan_t2v"})
    assert spec.job == {"template": "wan_t2v"}
    assert spec.timeout_s is None
    assert spec.cost_cap is None


def test_stub_env_name_and_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert stub_env_name("blender") == "STUDIO_STUB_BLENDER"
    assert stub_env_name("ComfyUI") == "STUDIO_STUB_COMFYUI"
    monkeypatch.delenv("STUDIO_STUB_BLENDER", raising=False)
    assert is_stub_mode("blender") is False
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "1")
    assert is_stub_mode("blender") is True
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "yes")
    assert is_stub_mode("blender") is True
    monkeypatch.setenv("STUDIO_STUB_BLENDER", "0")
    assert is_stub_mode("blender") is False


def test_dynamic_switch_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    assert is_dynamic_enabled() is False
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    assert is_dynamic_enabled() is True
    monkeypatch.setenv("STUDIO_DYNAMIC", "false")
    assert is_dynamic_enabled() is False


class _FakeEngine(Engine):
    name = "fake"
    tier = EngineTier.WARM

    def list(self) -> EngineResult:
        return EngineResult(ok=True, engine=self.name, verb="list", payload={"nodes": []})

    def install(self, submotor: str, *, ref: str | None = None) -> EngineResult:
        blocked = self._guard_install(submotor, baked=False)
        if blocked is not None:
            return blocked
        return EngineResult(
            ok=True, engine=self.name, verb="install", payload={"installed": submotor}
        )

    def verify(self, submotor: str) -> EngineResult:
        return EngineResult(ok=True, engine=self.name, verb="verify", payload={"ok": submotor})

    def run(self, spec: EngineRunSpec) -> EngineResult:
        return EngineResult(ok=True, engine=self.name, verb="run", payload={"job": spec.job})


def test_install_blocked_in_deterministic_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    eng = _FakeEngine()
    res = eng.install("some-node")
    assert res.ok is False
    assert "STUDIO_DYNAMIC=1" in (res.error or "")


def test_install_allowed_when_dynamic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    eng = _FakeEngine()
    res = eng.install("some-node")
    assert res.ok is True
    assert res.payload["installed"] == "some-node"


def test_fake_engine_implements_four_verbs() -> None:
    eng = _FakeEngine()
    assert eng.list().verb == "list"
    assert eng.verify("x").verb == "verify"
    assert eng.run(EngineRunSpec(job={})).verb == "run"
    assert eng.is_stub() is False
