"""Tests del motor LLM local / llama-server (loopback, OpenAI-compatible).

Sin GPU ni red: sondeo sin binario, sin modelo, contrato de entrada,
endpoint loopback-only y registro en el roster. Falla en main (sin el
adaptador) y pasa con el adaptador registrado.
"""

from __future__ import annotations

import pytest

from comfy_studio.engines import EngineRunSpec, EngineTier
from comfy_studio.engines.llm_server import (
    LlmServerEngine,
    is_loopback_url,
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_STUB_LLM_SERVER", raising=False)
    monkeypatch.setenv("STUDIO_LLM_SERVER_BIN", "")
    monkeypatch.delenv("STUDIO_LLM_SERVER_MODEL", raising=False)
    monkeypatch.delenv("STUDIO_LLM_SERVER_URL", raising=False)
    monkeypatch.setenv("PATH", "")


def test_tier_and_name() -> None:
    assert LlmServerEngine().name == "llm_server"
    assert LlmServerEngine().tier == EngineTier.WARM


def test_list_informa_sin_exigir(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    res = LlmServerEngine().list()
    assert res.ok is True and res.verb == "list"
    assert res.payload["available"] is False
    assert res.payload["openai_compatible"] is True
    assert res.payload["stub"] is False


def test_verify_sin_binario_es_tipado(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    res = LlmServerEngine().verify("")
    assert res.ok is False
    assert "llama-server" in (res.error or "")


def test_verify_sin_modelo_es_tipado(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _clear_env(monkeypatch)
    fake_bin = tmp_path / "llama-server"
    fake_bin.write_bytes(b"x")
    monkeypatch.setenv("STUDIO_LLM_SERVER_BIN", str(fake_bin))
    res = LlmServerEngine().verify(str(tmp_path / "no-existe.gguf"))
    assert res.ok is False
    assert "ausente" in (res.error or "")


def test_verify_stub_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("STUDIO_STUB_LLM_SERVER", "1")
    assert LlmServerEngine().verify("").ok is True


def test_install_siempre_rechazado_tipado(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    res = LlmServerEngine().install("llama-cpp")
    assert res.ok is False and res.verb == "install"
    assert "no está incluido" in (res.error or "")


def test_run_rechaza_spec_invalido() -> None:
    assert LlmServerEngine().run(EngineRunSpec(job={})).ok is False
    bad_role = EngineRunSpec(job={"messages": [{"role": "x", "content": "hola"}]})
    assert LlmServerEngine().run(bad_role).ok is False
    bad_tokens = EngineRunSpec(job={"prompt": "hola", "max_tokens": 0})
    assert LlmServerEngine().run(bad_tokens).ok is False


def test_run_sin_binario_degrada_honesto(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    res = LlmServerEngine().run(EngineRunSpec(job={"prompt": "hola"}))
    assert res.ok is False
    assert "no disponible" in (res.error or "")


def test_run_rechaza_endpoint_no_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _clear_env(monkeypatch)
    fake_bin = tmp_path / "llama-server"
    fake_bin.write_bytes(b"x")
    model = tmp_path / "m.gguf"
    model.write_bytes(b"gguf")
    monkeypatch.setenv("STUDIO_LLM_SERVER_BIN", str(fake_bin))
    monkeypatch.setenv("STUDIO_LLM_SERVER_MODEL", str(model))
    monkeypatch.setenv("STUDIO_LLM_SERVER_URL", "http://10.0.0.5:8080")
    res = LlmServerEngine().run(EngineRunSpec(job={"prompt": "hola"}))
    assert res.ok is False
    assert "loopback" in (res.error or "")


def test_run_stub_no_red(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_LLM_SERVER", "1")
    res = LlmServerEngine().run(EngineRunSpec(job={"prompt": "resume el plano"}))
    assert res.ok is True and res.simulated is True
    assert "plano" in res.payload["text"]
    res2 = LlmServerEngine().run(
        EngineRunSpec(job={"messages": [{"role": "user", "content": "hola"}]})
    )
    assert res2.ok is True and "hola" in res2.payload["text"]


def test_loopback_guard() -> None:
    assert is_loopback_url("http://127.0.0.1:8080") is True
    assert is_loopback_url("http://localhost:8080") is True
    assert is_loopback_url("http://[::1]:8080") is True
    assert is_loopback_url("http://10.0.0.5:8080") is False
    assert is_loopback_url("https://example.com") is False


def test_registrado_en_el_roster() -> None:
    from comfy_studio.engines.registry import (
        ENGINE_CLASSES,
        build_default_registry,
        resolve_engine_class,
    )

    reg = build_default_registry()
    assert "llm_server" in reg.names()
    assert "llm_server" in ENGINE_CLASSES
    cls = resolve_engine_class("llm_server")
    assert cls.name == "llm_server"
    assert cls.tier == reg.get("llm_server").tier
    for verb in ("list", "install", "verify", "run"):
        assert callable(getattr(cls, verb, None))
