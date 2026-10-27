"""Tests del loop del agente LLM, fallback DeepSeek, keyframes base64 y redacción."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.llm import (
    LLMProviderError,
    OpenAICompatibleLLMClient,
    encode_image_to_data_url,
)
from comfy_studio.agent.loop import StudioAgent
from comfy_studio.agent.tools import create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import SECRET_PLACEHOLDER, StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore
from comfy_studio.stubs.comfyui import ComfyUIStubState
from comfy_studio.stubs.llm import StubLLMClient, StubLLMstep, StubToolCallSpec


@pytest.mark.anyio
async def test_plan_tools_answer(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
    stub_comfyui_state: ComfyUIStubState,
) -> None:
    """Ciclo completo: instrucción -> plan + 2 tool calls válidas -> respuesta final."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Spot Astronauta")

    store.save_upload(
        project["id"],
        filename="gorra_verde_toma1.mp4",
        content_type="video/mp4",
        data=b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom",
        max_upload_bytes=1024 * 1024,
        category="uploads",
    )

    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Plan: primero busco el plano de la gorra verde y luego encolo el render VACE.",
            tool_calls=[
                StubToolCallSpec(
                    name="search_assets",
                    arguments={"query": "gorra_verde", "kind": "video"},
                ),
                StubToolCallSpec(
                    name="enqueue_render",
                    arguments={
                        "template_id": "vace_inpaint",
                        "params": {
                            "prompt": "Sujeto con traje de astronauta blanco en la misma escena",
                            "width": 832,
                            "height": 480,
                            "frames": 81,
                            "steps": 4,
                        },
                    },
                ),
            ],
        )
    )
    stub_llm.enqueue_step(
        StubLLMstep(
            content=(
                "Listo: encontré `gorra_verde_toma1.mp4` y encolé el render con `vace_inpaint`."
            ),
            tool_calls=[],
        )
    )

    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    llm_client = OpenAICompatibleLLMClient(
        config=studio_config,
        stub_client=stub_llm,
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=llm_client,
        tool_belt=create_default_tool_belt(),
    )

    streamed_events: list[dict[str, Any]] = []
    async for ev in agent.stream_turn(
        project_id=project["id"],
        user_message="Pon al sujeto de la gorra verde con traje de astronauta",
    ):
        streamed_events.append(ev)

    event_types = [e["type"] for e in streamed_events]
    assert "token" in event_types
    assert event_types.count("tool_call") == 2
    assert event_types.count("tool_result") == 2
    assert event_types[-1] == "done"

    done_ev = streamed_events[-1]
    assert "vace_inpaint" in done_ev["reply"]
    assert done_ev["iterations"] == 2
    assert len(stub_comfyui_state.prompts) == 1

    # Verificar también que los eventos se publicaron en el broker SSE del panel
    broker_types = [ev_type for ev_type, _ in broker.recent_events()]
    assert "token" in broker_types
    assert "tool_call" in broker_types
    assert "tool_result" in broker_types


@pytest.mark.anyio
async def test_fallback_on_500_4xx_and_timeout(
    studio_data_dir: Path,
) -> None:
    """Cuando el primario DashScope falla (500, 429 o timeout), entra el fallback DeepSeek."""
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        studio_llm_model="qwen3-vl-plus",
        studio_llm_fallback_base_url="https://api.deepseek.com",
        studio_llm_fallback_model="deepseek-chat",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_stub_llm=False,
    )

    for failure_mode in ("500", "429", "timeout"):
        called_urls: list[tuple[str, str]] = []

        async def handler(
            request: httpx.Request,
            _mode: str = failure_mode,
            _calls: list[tuple[str, str]] = called_urls,
        ) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            _calls.append((str(request.url), payload.get("model", "")))
            if "dashscope-intl" in str(request.url):
                if _mode == "timeout":
                    raise httpx.ReadTimeout("Primary DashScope timed out", request=request)
                status = 500 if _mode == "500" else 429
                return httpx.Response(
                    status_code=status,
                    json={"error": {"message": f"Simulated primary failure {status}"}},
                )
            # Respuesta del fallback DeepSeek
            return httpx.Response(
                status_code=200,
                json={
                    "id": "chatcmpl-fallback-1",
                    "model": "deepseek-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"Respuesta desde fallback DeepSeek tras {_mode}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        llm_client = OpenAICompatibleLLMClient(config=cfg, transport=transport)
        resp = await llm_client.complete(
            messages=[{"role": "user", "content": "Hola estudio"}],
        )

        assert resp.provider == "fallback"
        assert resp.model == "deepseek-chat"
        assert f"tras {failure_mode}" in resp.content
        assert len(called_urls) == 2
        assert "dashscope-intl.aliyuncs.com" in called_urls[0][0]
        assert called_urls[0][1] == "qwen3-vl-plus"
        assert "api.deepseek.com" in called_urls[1][0]
        assert called_urls[1][1] == "deepseek-chat"


@pytest.mark.anyio
async def test_keyframes_attached_as_base64_never_auth_url(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """Los keyframes se adjuntan como data URL base64 inline y jamás como URL con auth."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Keyframes Vision")

    raw_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    kf_asset = store.save_upload(
        project["id"],
        filename="keyframe_001.png",
        content_type="image/png",
        data=raw_png,
        max_upload_bytes=1024 * 1024,
        category="keyframes",
    )

    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
    )

    turn = await agent.run_turn(
        project_id=project["id"],
        user_message="¿Qué ves en este keyframe?",
        asset_ids=[kf_asset["id"]],
    )
    assert turn.reply

    assert len(stub_llm.calls) == 1
    sent_messages = stub_llm.calls[0]
    user_msg = [m for m in sent_messages if m["role"] == "user"][-1]
    content_parts = user_msg["content"]
    assert isinstance(content_parts, list)

    image_parts = [p for p in content_parts if p.get("type") == "image_url"]
    assert len(image_parts) == 1
    data_url = image_parts[0]["image_url"]["url"]
    expected_b64 = base64.b64encode(raw_png).decode("ascii")
    assert data_url == f"data:image/png;base64,{expected_b64}"
    assert "http://" not in data_url
    assert "https://" not in data_url
    assert "/studio/api/" not in data_url
    assert studio_config.get_comfy_pass() not in json.dumps(sent_messages)

    # Verificar que si alguien intenta inyectar una URL HTTP con auth para imagen se rechaza
    with pytest.raises(ValueError, match="base64"):
        await agent.run_turn(
            project_id=project["id"],
            user_message="Analiza esta URL prohibida",
            keyframe_data_urls=[
                "http://admin:changeme-test-pass-42@127.0.0.1:8188/studio/api/projects/x/assets/y/file"
            ],
        )


@pytest.mark.anyio
async def test_session_memory_persists_across_turns_per_project(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """La memoria de sesión persiste en SQLite por proyecto entre múltiples turnos."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    proj_a = store.create_project(name="Proyecto A")
    proj_b = store.create_project(name="Proyecto B")

    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
    )

    await agent.run_turn(
        project_id=proj_a["id"],
        user_message="El protagonista se llama Marco y lleva gorra verde.",
    )
    await agent.run_turn(
        project_id=proj_a["id"],
        user_message="Ahora haz que Marco salude a cámara.",
    )

    # La segunda llamada del Proyecto A debe incluir el historial del primer turno
    second_call_messages = stub_llm.calls[1]
    serialized_history = json.dumps(second_call_messages, ensure_ascii=False)
    assert "Marco" in serialized_history
    assert "gorra verde" in serialized_history

    # El Proyecto B tiene memoria aislada: no contiene los mensajes de A
    await agent.run_turn(
        project_id=proj_b["id"],
        user_message="Este es un documental de cocina.",
    )
    proj_b_messages = stub_llm.calls[2]
    assert "Marco" not in json.dumps(proj_b_messages, ensure_ascii=False)


@pytest.mark.anyio
async def test_max_iterations_guard_stops_loop(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """El loop de tool-calling respeta el máximo de iteraciones configurado."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Loop Limit Project")

    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    for _ in range(10):
        stub_llm.enqueue_step(
            StubLLMstep(
                content="Sigo buscando...",
                tool_calls=[
                    StubToolCallSpec(name="search_assets", arguments={"query": "clip"})
                ],
            )
        )

    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=broker,
        redactor=studio_config.redactor(),
    )
    agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
        max_iterations=3,
    )

    result = await agent.run_turn(
        project_id=project["id"],
        user_message="Busca clips infinitamente",
    )
    assert result.iterations == 3
    assert result.stop_reason == "max_iterations"
    assert len(stub_llm.calls) == 3


@pytest.mark.anyio
async def test_key_never_logged_or_leaked_in_status(
    studio_data_dir: Path,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """STUDIO_LLM_KEY jamás aparece en logs, errores del cliente ni en /studio/api/status."""
    raw_key = "custom-dashscope-secret-placeholder-991122"
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_key=raw_key,
        studio_stub_llm=False,
    )

    async def failing_handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        return httpx.Response(
            status_code=500,
            json={"error": {"message": f"Upstream rejected header {auth} for key {raw_key}"}},
        )

    llm_client = OpenAICompatibleLLMClient(
        config=cfg,
        transport=httpx.MockTransport(failing_handler),
    )

    with caplog.at_level(logging.DEBUG, logger="comfy_studio"):
        with pytest.raises(LLMProviderError) as exc_info:
            await llm_client.complete(
                messages=[{"role": "user", "content": "Prueba de fuga"}],
            )

    assert raw_key not in str(exc_info.value)
    assert raw_key not in caplog.text
    assert SECRET_PLACEHOLDER in str(exc_info.value) or SECRET_PLACEHOLDER in caplog.text

    status_resp = client.get("/studio/api/status")
    assert status_resp.status_code == 200
    assert raw_key not in status_resp.text
    assert "YOUR_STUDIO_API_KEY_HERE" not in status_resp.text
    assert status_resp.json()["llm"]["key_status"] == "<definida>"
    assert status_resp.json()["env_masked"]["STUDIO_LLM_KEY"] == "<definida>"


def test_chat_endpoints_json_and_streaming_sse(client: TestClient) -> None:
    """Verifica POST/GET /studio/api/projects/{id}/chat en modo JSON y SSE streaming."""
    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Chat HTTP"},
    ).json()
    pid = proj["id"]

    # 1. Turno JSON contra stub LLM
    chat_resp = client.post(
        f"/studio/api/projects/{pid}/chat",
        json={
            "message": "Propón un EDL de highlights para el clip inicial",
            "stream": False,
        },
    )
    assert chat_resp.status_code == 200
    body = chat_resp.json()
    assert body["project_id"] == pid
    assert isinstance(body["reply"], str) and body["reply"]

    # 2. Historial persistido en GET /studio/api/projects/{id}/chat
    hist_resp = client.get(f"/studio/api/projects/{pid}/chat")
    assert hist_resp.status_code == 200
    messages = hist_resp.json()["messages"]
    assert len(messages) >= 2
    assert messages[0]["role"] == "user"

    # 3. Turno con streaming SSE (stream=True)
    stream_resp = client.post(
        f"/studio/api/projects/{pid}/chat",
        json={
            "message": "Genera un vídeo corto de un faro con wan_t2v",
            "stream": True,
        },
    )
    assert stream_resp.status_code == 200
    assert stream_resp.headers["content-type"].startswith("text/event-stream")
    assert "event: token" in stream_resp.text
    assert "event: done" in stream_resp.text


def test_encode_image_to_data_url_helper() -> None:
    data = b"\x89PNG\r\n\x1a\n"
    url = encode_image_to_data_url(data, "image/png")
    assert url.startswith("data:image/png;base64,")
    assert "http" not in url
