"""Tests del adaptador ComfyUI al contrato de motores (M4-SCR-002, F2).

Usa un cliente async falso (sin ComfyUI vivo) y `asyncio.run` (sin añadir la
dependencia pytest-asyncio al pod): prueba list/verify/run/install y la guarda
STUDIO_DYNAMIC en install.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.comfyui import ComfyUIEngine


class _FakeOutput:
    def __init__(self, path: str) -> None:
        self._path = path

    def relative_path(self) -> str:
        return self._path


class _FakeResult:
    def __init__(self, paths: list[str]) -> None:
        self.outputs = [_FakeOutput(p) for p in paths]


class _FakeClient:
    """Cliente ComfyUI async mínimo para los tests."""

    def __init__(self, object_info: dict[str, Any] | None = None) -> None:
        self._info = object_info if object_info is not None else {"KSampler": {}, "VAEDecode": {}}
        self.last_graph: dict | None = None

    async def get_object_info(self, node_class: str | None = None) -> dict[str, Any]:
        if node_class is None:
            return self._info
        return {node_class: self._info[node_class]} if node_class in self._info else {}

    async def execute_prompt_job(
        self, graph: dict, *, job_id: str | None = None, project_id: str | None = None
    ) -> _FakeResult:
        self.last_graph = graph
        return _FakeResult(["render_0001.png"])


def test_list_returns_node_types() -> None:
    eng = ComfyUIEngine(_FakeClient())
    res = asyncio.run(eng.list())
    assert res.ok is True
    assert "KSampler" in res.payload["node_types"]
    assert res.payload["count"] == 2


def test_verify_present_and_missing() -> None:
    eng = ComfyUIEngine(_FakeClient())
    present = asyncio.run(eng.verify("KSampler"))
    assert present.ok is True and present.payload["present"] is True
    missing = asyncio.run(eng.verify("LoraTrainerThatDoesNotExist"))
    assert missing.ok is False
    assert "no instalado" in (missing.error or "")


def test_run_executes_graph() -> None:
    client = _FakeClient()
    eng = ComfyUIEngine(client)
    graph = {"1": {"class_type": "KSampler", "inputs": {}}}
    res = asyncio.run(eng.run(EngineRunSpec(job={"graph": graph})))
    assert res.ok is True
    assert res.payload["outputs"] == ["render_0001.png"]
    assert client.last_graph == graph


def test_run_rejects_empty_graph() -> None:
    eng = ComfyUIEngine(_FakeClient())
    res = asyncio.run(eng.run(EngineRunSpec(job={"graph": {}})))
    assert res.ok is False
    assert "no vacío" in (res.error or "")


def test_install_blocked_when_dynamic_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    eng = ComfyUIEngine(_FakeClient())
    res = asyncio.run(eng.install("some-node"))
    assert res.ok is False
    assert "STUDIO_DYNAMIC=1" in (res.error or "")


def test_install_pending_wiring_when_dynamic_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    eng = ComfyUIEngine(_FakeClient())
    res = asyncio.run(eng.install("some-node"))
    # dinámico activo pero endpoint de Manager aún no cableado (F6): falla tipado.
    assert res.ok is False
    assert "F6" in (res.error or "")
