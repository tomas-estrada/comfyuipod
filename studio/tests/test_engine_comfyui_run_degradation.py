"""Degradacion honesta del motor comfyui sin servidor (T2).

Sin ComfyUI vivo el cliente levanta `ComfyUIError`; `run` debe devolverlo
TIPADO (`ok=False`, `simulated=False`, mensaje `run falló: …`) y nunca fingir
exito ni marcarlo como simulado.
"""

from __future__ import annotations

import asyncio
from typing import Any

from comfy_studio.comfyui_client import ComfyUIError
from comfy_studio.engines import EngineRunSpec
from comfy_studio.engines.comfyui import ComfyUIEngine


class _DownClient:
    """ComfyUI caído: toda llamada levanta `ComfyUIError`."""

    async def get_object_info(self, node_class: str | None = None) -> dict[str, Any]:
        raise ComfyUIError("servidor ComfyUI no alcanzable")

    async def execute_prompt_job(
        self,
        graph: dict,
        *,
        job_id: str | None = None,
        project_id: str | None = None,
        cancel_event=None,
    ) -> Any:
        raise ComfyUIError("servidor ComfyUI no alcanzable")


def _graph() -> dict:
    return {"1": {"class_type": "KSampler", "inputs": {}}}


def test_run_without_server_fails_typed() -> None:
    eng = ComfyUIEngine(_DownClient())  # type: ignore[arg-type]
    res = asyncio.run(eng.run(EngineRunSpec(job={"graph": _graph()})))
    assert res.ok is False
    assert res.simulated is False
    assert "run falló" in (res.error or "")


def test_run_without_server_never_reports_success() -> None:
    eng = ComfyUIEngine(_DownClient())  # type: ignore[arg-type]
    res = asyncio.run(eng.run(EngineRunSpec(job={"graph": _graph()})))
    assert not (res.ok is True and res.simulated is False), "sin servidor no puede haber exito real"
    assert res.ok is False
