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

    async def _request(self, method, path, *, json_body=None, timeout=None, **kw):
        # Simula el Manager: registra la instalación y hace visible el node type.
        self.last_install = (method, path, json_body)
        node_id = (json_body or {}).get("id")
        if getattr(self, "_install_http_status", 200) < 400 and node_id:
            self._info[node_id] = {}
        return _FakeResp(getattr(self, "_install_http_status", 200))


class _FakeResp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


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


def test_install_real_success_when_dynamic_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    client = _FakeClient()
    eng = ComfyUIEngine(client)
    res = asyncio.run(eng.install("ComfyUI-SomeNode"))
    # Manager 200 + el node aparece en object_info -> install ok, visible.
    assert res.ok is True
    assert res.payload["installed"] == "ComfyUI-SomeNode"
    assert res.payload["visible"] is True
    assert client.last_install[0] == "POST"


def test_install_http_error_fails_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    client = _FakeClient()
    client._install_http_status = 500
    eng = ComfyUIEngine(client)
    res = asyncio.run(eng.install("ComfyUI-SomeNode"))
    assert res.ok is False
    assert "HTTP 500" in (res.error or "")
