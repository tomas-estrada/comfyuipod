"""Tests del tool-belt M3-B: list_templates, job_status, job_cancel, tag_asset,
system_status y export_edl (plan 1793155369645, tarea 8)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from comfy_studio.agent.tools import (
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

PNG_MAGIC = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 32

NEW_TOOLS = {
    "list_templates",
    "job_status",
    "job_cancel",
    "tag_asset",
    "system_status",
    "export_edl",
}


@pytest.fixture()
def tool_context(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> ToolContext:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(
        name="Proyecto Tools M3-B",
        description="Pruebas del tool-belt M3-B",
        aspect_ratio="16:9",
    )
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


def test_default_belt_registers_m3b_tools() -> None:
    belt = create_default_tool_belt()
    names = set(belt.list_names())
    assert NEW_TOOLS.issubset(names)
    specs = {spec.name: spec for spec in belt.list_specs()}
    for name in NEW_TOOLS:
        assert len(specs[name].description) >= 20
    for tool_def in belt.to_openai_tools():
        assert tool_def["type"] == "function"


@pytest.mark.anyio
async def test_list_templates_returns_curated_registry(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute("list_templates", {}, tool_context)
    assert result["count"] == 11
    ids = {t["id"] for t in result["templates"]}
    assert ids == {
        "vace_inpaint",
        "vace_restyle",
        "vace_swap",
        "wan_flf2v",
        "wan_i2v",
        "wan_i2v_5b",
        "wan_t2v",
        "wan_t2v_5b",
        "sdxl_image",
        "sdxl_ipadapter",
        "hunyuan3d_image_to_3d",
    }

    with_wf = await belt.execute("list_templates", {"include_workflow": True}, tool_context)
    assert with_wf["include_workflow"] is True
    assert all("workflow" in t for t in with_wf["templates"])


@pytest.mark.anyio
async def test_job_status_lists_and_filters_by_project(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    empty = await belt.execute("job_status", {}, tool_context)
    assert empty["count"] == 0
    assert empty["jobs"] == []

    job = tool_context.store.create_job(
        kind="render",
        project_id=tool_context.project_id,
        template_id="sdxl_image",
        status="queued",
    )
    other = tool_context.store.create_job(kind="render", project_id=None, status="queued")

    listed = await belt.execute("job_status", {}, tool_context)
    assert listed["count"] == 1
    assert listed["jobs"][0]["id"] == job["id"]

    single = await belt.execute("job_status", {"job_id": job["id"]}, tool_context)
    assert single["job"]["id"] == job["id"]

    with pytest.raises(ToolExecutionError):
        await belt.execute("job_status", {"job_id": other["id"]}, tool_context)
    with pytest.raises(ToolExecutionError):
        await belt.execute("job_status", {"job_id": "no-existe"}, tool_context)


@pytest.mark.anyio
async def test_job_cancel_marks_queued_job_cancelled(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    job = tool_context.store.create_job(
        kind="render",
        project_id=tool_context.project_id,
        status="queued",
    )
    result = await belt.execute("job_cancel", {"job_id": job["id"]}, tool_context)
    assert result["status"] == "cancelled"
    assert result["interrupted_active_run"] is False
    stored = tool_context.store.get_job(job["id"])
    assert stored is not None
    assert stored["status"] == "cancelled"

    again = await belt.execute("job_cancel", {"job_id": job["id"]}, tool_context)
    assert again["already_terminal"] is True


@pytest.mark.anyio
async def test_job_cancel_running_calls_interrupt(
    tool_context: ToolContext,
    stub_comfyui_state,
) -> None:
    belt = create_default_tool_belt()
    job = tool_context.store.create_job(
        kind="render",
        project_id=tool_context.project_id,
        status="running",
    )
    result = await belt.execute("job_cancel", {"job_id": job["id"]}, tool_context)
    assert result["status"] == "cancelled"
    assert result["interrupted_active_run"] is True
    assert result["interrupt_error"] is None


@pytest.mark.anyio
async def test_job_cancel_rejects_foreign_job(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    foreign = tool_context.store.create_job(kind="render", project_id=None, status="queued")
    with pytest.raises(ToolExecutionError):
        await belt.execute("job_cancel", {"job_id": foreign["id"]}, tool_context)


@pytest.mark.anyio
async def test_tag_asset_merges_tags_and_note(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    asset = tool_context.store.save_upload(
        tool_context.project_id,
        filename="toma.png",
        content_type="image/png",
        data=PNG_MAGIC,
        max_upload_bytes=1024 * 1024,
    )

    result = await belt.execute(
        "tag_asset",
        {"asset_id": asset["id"], "tags": ["toma-buena", " exterior "], "note": "Plano general"},
        tool_context,
    )
    assert result["status"] == "tagged"
    meta = result["asset"]["metadata"]
    assert meta["tags"] == ["toma-buena", "exterior"]
    assert meta["note"] == "Plano general"

    second = await belt.execute(
        "tag_asset",
        {"asset_id": asset["id"], "tags": ["toma-buena", "noche"]},
        tool_context,
    )
    assert second["asset"]["metadata"]["tags"] == ["toma-buena", "exterior", "noche"]
    assert second["asset"]["metadata"]["note"] == "Plano general"

    with pytest.raises(ToolExecutionError):
        await belt.execute("tag_asset", {"asset_id": "inexistente", "tags": []}, tool_context)


@pytest.mark.anyio
async def test_system_status_reports_jobs_and_supervisor(
    tool_context: ToolContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    belt = create_default_tool_belt()
    tool_context.store.create_job(
        kind="render",
        project_id=tool_context.project_id,
        status="done",
    )
    tool_context.store.create_job(
        kind="render",
        project_id=tool_context.project_id,
        status="failed",
    )

    sup_file = tmp_path / "supervisor.json"
    sup_file.write_text(
        json.dumps({"state": "running", "restarts": 0, "healthy_resets": 2}),
        encoding="utf-8",
    )
    monkeypatch.setenv("COMFY_SUPERVISOR_JSON", str(sup_file))

    result = await belt.execute("system_status", {}, tool_context)
    assert result["supervisor"] == {"state": "running", "restarts": 0, "healthy_resets": 2}
    assert result["total_jobs"] == 2
    assert result["jobs_by_status"] == {"done": 1, "failed": 1}
    assert isinstance(result["comfyui_queue"], dict)

    monkeypatch.setenv("COMFY_SUPERVISOR_JSON", str(tmp_path / "missing.json"))
    degraded = await belt.execute("system_status", {}, tool_context)
    assert degraded["supervisor"] is None


@pytest.mark.anyio
async def test_export_edl_returns_latest_and_version(tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError):
        await belt.execute("export_edl", {}, tool_context)

    proposed = await belt.execute(
        "propose_edl",
        {
            "title": "Highlights v1",
            "aspect_ratio": "16:9",
            "cuts": [{"in_sec": 0.0, "out_sec": 2.5, "reason": "intro"}],
        },
        tool_context,
    )
    v1 = proposed["version"]

    exported = await belt.execute("export_edl", {}, tool_context)
    assert exported["status"] == "exported"
    assert exported["version"] == v1
    assert exported["edl"]["title"] == "Highlights v1"
    assert exported["output_asset"] is None

    by_version = await belt.execute("export_edl", {"version": v1}, tool_context)
    assert by_version["edl_id"] == exported["edl_id"]

    with pytest.raises(ToolExecutionError):
        await belt.execute("export_edl", {"version": 42}, tool_context)
