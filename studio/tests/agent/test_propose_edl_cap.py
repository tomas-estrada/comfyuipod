"""Invariante: `propose_edl` acota los cortes como el endpoint HTTP de EDL.

Causa (pre-fix): `ProposeEdlArgs.cuts` solo pedía `min_length=1`, sin
`max_length`, mientras `EdlCreateRequest` / `EdlBuildRequest` acotan a 200
(`M3-SCR-005`: un corte = un recorte + concat ffmpeg). El agente podía
persistir un EDL con miles de cortes y `build_edl` encadenaría miles de
subprocesos, colgando el turno.

El test verifica la frontera 200/201 contra el tool-belt real.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from comfy_studio.agent.tools import ToolContext, ToolExecutionError, create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


def _cuts(n: int) -> list[dict[str, object]]:
    return [
        {"in_sec": float(i * 2), "out_sec": float(i * 2 + 1), "reason": "highlight"}
        for i in range(n)
    ]


def _ctx(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> ToolContext:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto edl-cap")
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=studio_config,
    )


@pytest.mark.anyio
async def test_propose_edl_accepts_boundary_200_cuts(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    ctx = _ctx(studio_config, studio_data_dir, stub_comfyui_transport)
    belt = create_default_tool_belt()
    res = await belt.execute(
        "propose_edl",
        {"title": "Frontera 200", "cuts": _cuts(200)},
        ctx,
    )
    assert res["status"] == "proposed"
    assert res["cuts_count"] == 200


@pytest.mark.anyio
async def test_propose_edl_rejects_201_cuts(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    ctx = _ctx(studio_config, studio_data_dir, stub_comfyui_transport)
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "propose_edl",
            {"title": "Demasiados", "cuts": _cuts(201)},
            ctx,
        )
    # Nada persistido en la versión rechaza: sigue sin EDL.
    assert ctx.store.get_latest_edl(ctx.project_id) is None
