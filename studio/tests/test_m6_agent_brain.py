"""Tests del cerebro del agente, Bloque 1 fase 1 (M6-SCR).

Verifica el contrato tipado, el modo stub determinista y honesto, el registro y
el reporte de salud. Sin pesos/LLM/servicio: todo corre en stub.
"""

from __future__ import annotations

import pytest

from comfy_studio.agent_brain import (
    BRAIN_CAPABILITIES,
    BrainResult,
    is_brain_stub,
    stub_env_name,
)
from comfy_studio.agent_brain.capabilities import (
    CompressCapability,
    MemoryCapability,
    RerankCapability,
    RetrieveCapability,
)
from comfy_studio.agent_brain.registry import (
    brain_health_report,
    build_default_brain,
)

_ALL = ("memory", "compress", "retrieve", "rerank")


def _stub_all(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ALL:
        monkeypatch.setenv(stub_env_name(name), "1")


def test_capability_set_matches() -> None:
    assert BRAIN_CAPABILITIES == set(_ALL)


def test_stub_env_name_and_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert stub_env_name("memory") == "STUDIO_STUB_BRAIN_MEMORY"
    assert not is_brain_stub("memory")
    monkeypatch.setenv("STUDIO_STUB_BRAIN_MEMORY", "1")
    assert is_brain_stub("memory")


def test_compress_stub_is_honest_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BRAIN_COMPRESS", "1")
    cap = CompressCapability()
    res = cap.run("compress", {"text": "hola mundo de prueba"})
    assert res.ok and res.simulated
    # El stub NO finge compresión: devuelve el texto tal cual, ratio 1.0.
    assert res.payload["compressed"] == "hola mundo de prueba"
    assert res.payload["ratio"] == 1.0


def test_retrieve_stub_returns_top_k_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BRAIN_RETRIEVE", "1")
    cap = RetrieveCapability()
    res = cap.run("search", {"docs": ["a", "b", "c", "d"], "k": 2})
    assert res.ok and res.simulated
    assert res.payload["hits"] == ["a", "b"]
    assert res.payload["scored"] is False


def test_rerank_stub_is_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BRAIN_RERANK", "1")
    cap = RerankCapability()
    res = cap.run("rerank", {"candidates": ["x", "y", "z"], "k": 2})
    assert res.ok and res.simulated
    assert res.payload["ranked"] == ["x", "y"]
    assert res.payload["reordered"] is False


def test_memory_stub_remember_and_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BRAIN_MEMORY", "1")
    cap = MemoryCapability()
    assert cap.run("remember", {"key": "paleta", "value": "neón"}).payload["stored"] is True
    # recall en stub NO inventa memorias: lista vacía honesta.
    assert cap.run("recall", {"query": "paleta"}).payload["memories"] == []


def test_unknown_op_is_typed_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_STUB_BRAIN_COMPRESS", "1")
    res = CompressCapability().run("inventada", {})
    assert isinstance(res, BrainResult)
    assert res.ok is False
    assert "op desconocida" in (res.error or "")


def test_non_stub_fails_typed_not_raises() -> None:
    # Sin stub y sin backend real (llmlingua no instalado en CI), falla TIPADO, nunca lanza.
    res = CompressCapability().run("compress", {"text": "x"})
    assert res.ok is False
    assert res.error is not None
    assert "LLMLingua-2" in res.error


def test_registry_has_all_four_and_describe() -> None:
    reg = build_default_brain()
    assert set(reg.names()) == set(_ALL)
    desc = reg.describe()
    assert desc["count"] == 4


def test_registry_rejects_duplicate() -> None:
    reg = build_default_brain()
    with pytest.raises(ValueError, match="duplicada"):
        reg.register(CompressCapability())


def test_health_report_counts_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_all(monkeypatch)
    rep = brain_health_report(build_default_brain())
    assert rep["counts"]["stub"] == 4
    assert rep["counts"]["ready"] == 0
    assert rep["counts"]["unavailable"] == 0


def test_health_report_counts_unavailable_without_stub() -> None:
    rep = brain_health_report(build_default_brain())
    # Sin stub: memory está READY (SQLite stdlib siempre disponible); compress/
    # retrieve/rerank quedan unavailable en CI (sus libs no están instaladas).
    assert rep["counts"]["ready"] == 1
    assert rep["counts"]["unavailable"] == 3


def test_memory_real_sqlite_roundtrip(tmp_path) -> None:
    # Backend real (sin stub): remember -> recall sobre SQLite stdlib.
    cap = MemoryCapability()
    db = str(tmp_path / "mem.db")
    stored = cap.run("remember", {"db_path": db, "key": "paleta", "value": "neón azul"})
    assert stored.ok and stored.simulated is False
    got = cap.run("recall", {"db_path": db, "query": "paleta"})
    assert got.ok and got.simulated is False
    assert got.payload["memories"] == [{"key": "paleta", "value": "neón azul"}]
    # recall sin coincidencia -> lista vacía honesta.
    none = cap.run("recall", {"db_path": db, "query": "inexistente"})
    assert none.payload["memories"] == []


def test_memory_real_requires_db_path() -> None:
    res = MemoryCapability().run("remember", {"key": "x", "value": "y"})
    assert res.ok is False
    assert "db_path" in (res.error or "")


def test_memory_real_sqlite_updated_at_float(tmp_path) -> None:
    import sqlite3
    import time

    cap = MemoryCapability()
    db = str(tmp_path / "mem_time.db")
    before = time.time() - 0.5
    cap.run("remember", {"db_path": db, "key": "k1", "value": "v1"})
    after = time.time() + 0.5
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT updated_at FROM agent_memory WHERE key = 'k1'").fetchone()
        assert row is not None
        ts = row[0]
        assert isinstance(ts, float)
        assert before <= ts <= after
