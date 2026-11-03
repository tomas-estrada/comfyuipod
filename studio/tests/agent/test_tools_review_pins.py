"""Tests de las herramientas de agente de review pins (M3-RVP-001)."""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_studio.agent.tools import (
    ListReviewPinsArgs,
    ResolveReviewPinArgs,
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def pins_tool_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ToolContext:
    monkeypatch.setenv("STUDIO_STUB_LLM", "1")
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Pins RVP")
    comfy_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=None,
        store=store,
        broker=broker,
        redactor=cfg.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=cfg,
    )


def _seed_edl_and_pin(ctx: ToolContext) -> dict:
    edl = {
        "schema": "comfy_studio.edl.v1",
        "title": "EDL Pins",
        "aspect_ratio": "16:9",
        "total_duration_sec": 5.0,
        "cuts": [
            {"in_sec": 0.0, "out_sec": 5.0, "reason": "highlight"},
        ],
        "notes": "",
    }
    saved = ctx.store.save_edl_version(ctx.project_id, edl=edl)
    pin = ctx.store.create_review_pin(
        ctx.project_id,
        edl_version=saved["version"],
        t_start=1.0,
        t_end=2.0,
        severity="issue",
        body="Suavizar el corte.",
    )
    return {"edl": saved, "pin": pin}


@pytest.mark.anyio
async def test_list_review_pins_matches_cut(pins_tool_context: ToolContext) -> None:
    seeded = _seed_edl_and_pin(pins_tool_context)
    belt = create_default_tool_belt()
    result = await belt.execute(
        "list_review_pins",
        {"edl_version": seeded["edl"]["version"], "status": "open"},
        pins_tool_context,
    )
    assert result["summary"] == {"note": 0, "issue": 1, "blocker": 0, "open": 1}
    assert len(result["pins"]) == 1
    assert result["pins"][0]["matched_cut"]["out_sec"] == 5.0


@pytest.mark.anyio
async def test_resolve_review_pin_flow(pins_tool_context: ToolContext) -> None:
    seeded = _seed_edl_and_pin(pins_tool_context)
    belt = create_default_tool_belt()
    result = await belt.execute(
        "resolve_review_pin",
        {
            "pin_id": seeded["pin"]["id"],
            "resolution_note": "Regenerado como v2 con transición.",
        },
        pins_tool_context,
    )
    assert result["status"] == "resolved"
    assert result["pin"]["resolved_by"] == "agent"
    assert result["pin"]["resolution_note"].startswith("Regenerado")

    # Ya no es resoluble
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "resolve_review_pin",
            {"pin_id": seeded["pin"]["id"]},
            pins_tool_context,
        )


@pytest.mark.anyio
async def test_resolve_review_pin_unknown_pin(pins_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "resolve_review_pin",
            {"pin_id": "pin-inexistente"},
            pins_tool_context,
        )


def test_args_models_reject_extra_fields() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError):
        ListReviewPinsArgs(edl_version=1, bogus=True)  # type: ignore[call-arg]
    with _pytest.raises(ValueError):
        ResolveReviewPinArgs(pin_id="pin-1", bogus=True)  # type: ignore[call-arg]
