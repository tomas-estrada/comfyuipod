"""Tests unitarios y de integración para el tool-belt v1 del agente (VAL-SERVER-004)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.tools import (
    ToolContext,
    ToolExecutionError,
    create_default_tool_belt,
)
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.comfyui import ComfyUIStubState


@pytest.fixture()
def tool_context(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport,
) -> ToolContext:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(
        name="Proyecto Tools v1",
        description="Pruebas del tool-belt v1",
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


def test_tool_belt_v1_schemas_are_valid_openai_functions() -> None:
    belt = create_default_tool_belt()
    openai_tools = belt.to_openai_tools()
    names = {t["function"]["name"] for t in openai_tools}
    assert {"enqueue_render", "search_assets", "propose_edl"}.issubset(names)

    for tool_def in openai_tools:
        assert tool_def["type"] == "function"
        fn = tool_def["function"]
        assert isinstance(fn["name"], str) and fn["name"]
        assert isinstance(fn["description"], str) and len(fn["description"]) >= 20
        params = fn["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


@pytest.mark.anyio
async def test_enqueue_render_valid_template(
    tool_context: ToolContext,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "enqueue_render",
        {
            "template_id": "wan_t2v",
            "params": {
                "prompt": "Un astronauta con gorra verde caminando bajo la lluvia neon",
                "width": 832,
                "height": 480,
                "frames": 81,
                "steps": 4,
            },
        },
        tool_context,
    )

    assert result["status"] in ("done", "completed", "queued", "running")
    assert result["template_id"] == "wan_t2v"
    assert result["job_id"]
    assert result["prompt_id"]
    assert len(stub_comfyui_state.prompts) == 1

    job = tool_context.store.get_job(result["job_id"])
    assert job is not None
    assert job["template_id"] == "wan_t2v"
    assert job["prompt_id"] == result["prompt_id"]

    # Verificar también vace_inpaint con defaults 480p
    res_vace = await belt.execute(
        "enqueue_render",
        {
            "template_id": "vace_inpaint",
            "params": {
                "prompt": "Traje de astronauta blanco detallado",
            },
        },
        tool_context,
    )
    assert res_vace["template_id"] == "vace_inpaint"
    assert res_vace["params"]["height"] == 480
    assert len(stub_comfyui_state.prompts) == 2


@pytest.mark.anyio
async def test_enqueue_render_invalid_params(
    tool_context: ToolContext,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    belt = create_default_tool_belt()

    # 1. Plantilla inexistente -> error sin enviar a ComfyUI
    with pytest.raises(ToolExecutionError) as exc_tpl:
        await belt.execute(
            "enqueue_render",
            {
                "template_id": "plantilla_no_existe",
                "params": {"prompt": "test"},
            },
            tool_context,
        )
    assert "plantilla" in str(exc_tpl.value).lower() or "template" in str(exc_tpl.value).lower()
    assert len(stub_comfyui_state.prompts) == 0

    # 2. Prompt vacío o dimensiones inválidas -> error sin enviar a ComfyUI
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "enqueue_render",
            {
                "template_id": "wan_t2v",
                "params": {
                    "prompt": "   ",
                    "width": -64,
                    "steps": 0,
                },
            },
            tool_context,
        )
    assert len(stub_comfyui_state.prompts) == 0


@pytest.mark.anyio
async def test_search_assets_filters_by_query_kind_and_category(
    tool_context: ToolContext,
) -> None:
    pid = tool_context.project_id
    store = tool_context.store

    store.save_upload(
        pid,
        filename="plano_gorra_verde.png",
        content_type="image/png",
        data=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        max_upload_bytes=1024 * 1024,
        category="keyframes",
        metadata={"scene": "sujeto con gorra verde en estudio"},
    )
    store.save_upload(
        pid,
        filename="entrevista_toma1.mp4",
        content_type="video/mp4",
        data=b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        max_upload_bytes=1024 * 1024,
        category="uploads",
    )
    store.save_upload(
        pid,
        filename="referencia_voz.wav",
        content_type="audio/wav",
        data=b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00",
        max_upload_bytes=1024 * 1024,
        category="voice",
    )

    belt = create_default_tool_belt()

    # Búsqueda por texto "gorra verde"
    found = await belt.execute(
        "search_assets",
        {"query": "gorra verde"},
        tool_context,
    )
    assert found["count"] == 1
    assert found["assets"][0]["original_filename"] == "plano_gorra_verde.png"

    # Filtro por kind="video"
    videos = await belt.execute(
        "search_assets",
        {"kind": "video"},
        tool_context,
    )
    assert videos["count"] == 1
    assert videos["assets"][0]["kind"] == "video"

    # Filtro por category="voice"
    voices = await belt.execute(
        "search_assets",
        {"category": "voice"},
        tool_context,
    )
    assert voices["count"] == 1
    assert voices["assets"][0]["category"] == "voice"


@pytest.mark.anyio
async def test_propose_edl_validates_and_persists_versioned_edl(
    tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()

    res1 = await belt.execute(
        "propose_edl",
        {
            "title": "Corte Highlights Reel",
            "aspect_ratio": "9:16",
            "cuts": [
                {
                    "source": "entrevista_toma1.mp4",
                    "in_sec": 1.5,
                    "out_sec": 5.0,
                    "reason": "highlight",
                    "speaker": "SPEAKER_00",
                },
                {
                    "source": "entrevista_toma1.mp4",
                    "in_sec": 8.0,
                    "out_sec": 12.5,
                    "reason": "highlight",
                    "speaker": "SPEAKER_01",
                },
            ],
            "notes": "Eliminadas pausas y muletillas entre 5.0s y 8.0s",
        },
        tool_context,
    )
    assert res1["status"] == "proposed"
    assert res1["version"] == 1
    assert res1["aspect_ratio"] == "9:16"
    assert res1["cuts_count"] == 2
    assert res1["total_duration_sec"] == pytest.approx(8.0)

    # Segunda propuesta incrementa versión a 2
    res2 = await belt.execute(
        "propose_edl",
        {
            "title": "Corte Highlights Reel v2",
            "aspect_ratio": "9:16",
            "cuts": [
                {
                    "source": "entrevista_toma1.mp4",
                    "in_sec": 2.0,
                    "out_sec": 5.0,
                    "reason": "highlight",
                }
            ],
        },
        tool_context,
    )
    assert res2["version"] == 2
    assert res2["total_duration_sec"] == pytest.approx(3.0)

    # Corte inválido (out_sec <= in_sec) debe fallar validación
    with pytest.raises(ToolExecutionError):
        await belt.execute(
            "propose_edl",
            {
                "aspect_ratio": "16:9",
                "cuts": [
                    {
                        "source": "entrevista_toma1.mp4",
                        "in_sec": 10.0,
                        "out_sec": 4.0,
                        "reason": "highlight",
                    }
                ],
            },
            tool_context,
        )


def test_tools_endpoint_lists_schemas(client: TestClient) -> None:
    resp = client.get("/studio/api/agent/tools")
    assert resp.status_code == 200
    body = resp.json()
    names = {t["function"]["name"] for t in body["tools"]}
    assert {"enqueue_render", "search_assets", "propose_edl"}.issubset(names)


@pytest.mark.anyio
async def test_enqueue_render_history_succeeds_view_fails_marks_job_error_and_emits_sse_error(
    tool_context: ToolContext,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    """Regresión M2-SCR-002 en tool enqueue_render del agente: si /history completa pero GET /view
    falla, el job queda en status=error con SSE de error (nunca done) y galería vacía.
    """
    from comfy_studio.comfyui_client import ComfyUIError

    stub_comfyui_state.fail_view_status = 404
    stub_comfyui_state.fail_view_error = "Output clip not found on /view"

    belt = create_default_tool_belt()
    with pytest.raises(ComfyUIError):
        await belt.execute(
            "enqueue_render",
            {
                "template_id": "wan_t2v",
                "params": {
                    "prompt": "Plano nocturno con lluvia",
                    "width": 832,
                    "height": 480,
                    "frames": 81,
                    "steps": 4,
                },
            },
            tool_context,
        )

    jobs = tool_context.store.list_jobs(project_id=tool_context.project_id)
    assert len(jobs) == 1
    failed_job = jobs[0]
    assert failed_job["status"] == "error"
    assert failed_job["error"]
    assert failed_job["prompt_id"] is not None

    assert tool_context.broker is not None
    events = tool_context.broker.recent_events()
    job_events = [
        payload
        for ev_type, payload in events
        if ev_type in ("job", "progress") and payload.get("job_id") == failed_job["id"]
    ]
    assert any(p.get("status") == "error" for p in job_events)
    assert not any(p.get("status") == "done" for p in job_events)

    outputs = tool_context.store.list_assets(tool_context.project_id, category="outputs")
    assert outputs == []

