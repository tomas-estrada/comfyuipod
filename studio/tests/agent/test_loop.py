"""Tests del loop del agente LLM, fallback DeepSeek, keyframes base64 y redacción."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
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
    """Cuando el primario DashScope falla (500, 429 o timeout), entra el fallback con su key."""
    primary_key = "YOUR_STUDIO_PRIMARY_KEY_1111"
    fallback_key = "YOUR_STUDIO_FALLBACK_KEY_2222"
    cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        studio_llm_model="qwen3-vl-plus",
        studio_llm_fallback_base_url="https://api.deepseek.com",
        studio_llm_fallback_model="deepseek-chat",
        studio_llm_key=primary_key,
        studio_llm_fallback_key=fallback_key,
        studio_stub_llm=False,
    )

    for failure_mode in ("500", "429", "timeout"):
        called_urls: list[tuple[str, str, str]] = []

        async def handler(
            request: httpx.Request,
            _mode: str = failure_mode,
            _calls: list[tuple[str, str, str]] = called_urls,
        ) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            auth = request.headers.get("Authorization", "")
            _calls.append((str(request.url), payload.get("model", ""), auth))
            if "dashscope-intl" in str(request.url):
                assert auth == f"Bearer {primary_key}"
                if _mode == "timeout":
                    raise httpx.ReadTimeout("Primary DashScope timed out", request=request)
                status = 500 if _mode == "500" else 429
                return httpx.Response(
                    status_code=status,
                    json={"error": {"message": f"Simulated primary failure {status}"}},
                )
            # El fallback DeepSeek exige su credencial propia y rechaza la primaria
            if auth != f"Bearer {fallback_key}":
                return httpx.Response(
                    status_code=401,
                    json={"error": {"message": "Invalid or missing fallback credential"}},
                )
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
        assert called_urls[0][2] == f"Bearer {primary_key}"
        assert "api.deepseek.com" in called_urls[1][0]
        assert called_urls[1][1] == "deepseek-chat"
        assert called_urls[1][2] == f"Bearer {fallback_key}"
        assert primary_key not in called_urls[1][2]


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


class _GatedSSEStream(httpx.AsyncByteStream):
    """Stream SSE que emite el primer chunk y espera a que el test libere el chunk final."""

    def __init__(
        self,
        first_chunk_bytes: bytes,
        final_chunk_bytes: bytes,
        release_final_chunk: asyncio.Event,
    ) -> None:
        self._first = first_chunk_bytes
        self._final = final_chunk_bytes
        self._release = release_final_chunk
        self.final_released_when_first_emitted = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        await asyncio.wait_for(self._release.wait(), timeout=2.0)
        yield self._final


class _GatedSSETransport(httpx.AsyncBaseTransport):
    """Transporte HTTP que valida stream=True y devuelve SSE incremental."""

    def __init__(self, release_final_chunk: asyncio.Event) -> None:
        self.release_final_chunk = release_final_chunk
        self.requests_payloads: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw_body = await request.aread()
        payload = json.loads(raw_body.decode("utf-8"))
        self.requests_payloads.append(payload)
        first_sse = (
            b'data: {"id":"cmpl-1","model":"qwen3-vl-plus","choices":[{"index":0,'
            b'"delta":{"role":"assistant","content":"Primer "},"finish_reason":null}]}\n\n'
        )
        final_sse = (
            b'data: {"id":"cmpl-1","model":"qwen3-vl-plus","choices":[{"index":0,'
            b'"delta":{"content":"token en vivo."},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(
            status_code=200,
            headers={"Content-Type": "text/event-stream; charset=utf-8"},
            stream=_GatedSSEStream(first_sse, final_sse, self.release_final_chunk),
            request=request,
        )


@pytest.mark.anyio
async def test_real_incremental_streaming_delivers_first_token_before_final_chunk(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """M2-SCR-003: El primer token llega ANTES de que el proveedor/stub libere su chunk final."""
    store = ProjectStore(studio_data_dir)
    comfy_client = ComfyUIClient(
        base_url=studio_config.studio_comfyui_url,
        transport=stub_comfyui_transport,
        store=store,
        broker=ProgressBroker(),
        redactor=studio_config.redactor(),
    )

    # 1. Prueba contra el transporte SSE compatible-OpenAI (verifica stream=True en HTTP)
    http_broker = ProgressBroker()
    http_project = store.create_project(name="Streaming Real HTTP SSE")
    http_gate = asyncio.Event()
    sse_transport = _GatedSSETransport(http_gate)
    http_cfg = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_stub_llm=False,
    )
    http_agent = StudioAgent(
        config=http_cfg,
        store=store,
        comfyui_client=comfy_client,
        broker=http_broker,
        llm_client=OpenAICompatibleLLMClient(config=http_cfg, transport=sse_transport),
    )

    turn_iter = http_agent.stream_turn(
        project_id=http_project["id"],
        user_message="Genera una respuesta incremental larga",
    ).__aiter__()

    # El primer evento token DEBE entregarse mientras http_gate sigue cerrado
    first_ev = await asyncio.wait_for(turn_iter.__anext__(), timeout=1.0)
    assert not http_gate.is_set(), "El chunk final aún no se ha liberado"
    assert first_ev["type"] == "token"
    assert first_ev["delta"] == "Primer "
    assert sse_transport.requests_payloads[0].get("stream") is True

    # El ProgressBroker también debe haber recibido ya el primer token en vivo
    broker_events_so_far = http_broker.recent_events()
    assert any(
        ev_type == "token" and payload.get("delta") == "Primer "
        for ev_type, payload in broker_events_so_far
    )

    # Liberamos el chunk final y consumimos el resto del turno
    http_gate.set()
    remaining_http: list[dict[str, Any]] = []
    async for ev in turn_iter:
        remaining_http.append(ev)

    assert any(e["type"] == "token" and e["delta"] == "token en vivo." for e in remaining_http)
    assert remaining_http[-1]["type"] == "done"
    assert remaining_http[-1]["reply"] == "Primer token en vivo."

    # 2. Prueba en modo STUDIO_STUB_LLM=1 con StubLLMClient bloqueando su chunk final
    stub_broker = ProgressBroker()
    stub_project = store.create_project(name="Streaming Real Stub")
    stub_gate = asyncio.Event()
    stub_llm = StubLLMClient(model=studio_config.studio_llm_model)
    stub_llm.enqueue_step(
        StubLLMstep(
            content="Inicio en vivo y cierre diferido.",
            chunks=["Inicio en vivo ", "y cierre diferido."],
            release_final_chunk=stub_gate,
        )
    )
    stub_agent = StudioAgent(
        config=studio_config,
        store=store,
        comfyui_client=comfy_client,
        broker=stub_broker,
        llm_client=OpenAICompatibleLLMClient(config=studio_config, stub_client=stub_llm),
    )

    stub_iter = stub_agent.stream_turn(
        project_id=stub_project["id"],
        user_message="Prueba streaming con stub",
    ).__aiter__()

    first_stub_ev = await asyncio.wait_for(stub_iter.__anext__(), timeout=1.0)
    assert not stub_gate.is_set()
    assert first_stub_ev["type"] == "token"
    assert first_stub_ev["delta"] == "Inicio en vivo "
    assert any(
        ev_type == "token" and payload.get("delta") == "Inicio en vivo "
        for ev_type, payload in stub_broker.recent_events()
    )

    stub_gate.set()
    remaining_stub = [ev async for ev in stub_iter]
    assert any(
        e["type"] == "token" and e["delta"] == "y cierre diferido." for e in remaining_stub
    )
    assert remaining_stub[-1]["type"] == "done"
    assert remaining_stub[-1]["reply"] == "Inicio en vivo y cierre diferido."


@pytest.mark.anyio
async def test_fallback_uses_distinct_key_and_fails_closed_when_missing(
    studio_data_dir: Path,
) -> None:
    """M2-SCR-004: El fallback usa STUDIO_LLM_FALLBACK_KEY y es fail-closed si falta."""
    primary_key = "PRIMARY_DASHSCOPE_SYNTH_KEY_AAAA"
    fallback_key = "FALLBACK_DEEPSEEK_SYNTH_KEY_BBBB"

    # 1. Fail-closed en modo HTTP si falta STUDIO_LLM_FALLBACK_KEY: no contacta al fallback
    cfg_no_fb = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_key=primary_key,
        studio_llm_fallback_key=None,
        studio_stub_llm=False,
    )
    fallback_requests: list[httpx.Request] = []

    async def fail_primary_handler(request: httpx.Request) -> httpx.Response:
        if "deepseek" in str(request.url):
            fallback_requests.append(request)
            return httpx.Response(status_code=200, json={"choices": []})
        return httpx.Response(status_code=502, json={"error": {"message": "Bad gateway"}})

    client_no_fb = OpenAICompatibleLLMClient(
        config=cfg_no_fb,
        transport=httpx.MockTransport(fail_primary_handler),
    )
    with pytest.raises(LLMProviderError, match="STUDIO_LLM_FALLBACK_KEY"):
        await client_no_fb.complete(messages=[{"role": "user", "content": "Hola"}])
    assert fallback_requests == [], "Nunca debe invocarse el proveedor fallback sin su propia key"

    with pytest.raises(LLMProviderError, match="STUDIO_LLM_FALLBACK_KEY"):
        await client_no_fb.stream_complete(messages=[{"role": "user", "content": "Hola"}])
    assert fallback_requests == []

    # 2. En modo stub (STUDIO_STUB_LLM=1), el doble exige credenciales sintéticas distintas
    stub_ok = StubLLMClient(
        model="qwen3-vl-plus",
        expected_primary_key=primary_key,
        expected_fallback_key=fallback_key,
    )
    stub_ok.enqueue_step(StubLLMstep(fail_with_status=503, error_message="Primary overloaded"))
    stub_ok.enqueue_step(StubLLMstep(content="Recuperado por fallback con credencial propia."))

    cfg_with_fb = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_key=primary_key,
        studio_llm_fallback_key=fallback_key,
        studio_stub_llm=True,
    )
    client_stub_ok = OpenAICompatibleLLMClient(config=cfg_with_fb, stub_client=stub_ok)
    resp = await client_stub_ok.stream_complete(messages=[{"role": "user", "content": "Hola"}])
    assert resp.provider == "fallback"
    assert resp.model == "deepseek-chat"
    assert resp.content == "Recuperado por fallback con credencial propia."
    assert stub_ok.auth_seen == [
        ("primary", f"Bearer {primary_key}"),
        ("fallback", f"Bearer {fallback_key}"),
    ]

    # 3. Si falta STUDIO_LLM_FALLBACK_KEY, el fallback en stub también falla cerrado
    stub_missing = StubLLMClient(model="qwen3-vl-plus")
    stub_missing.enqueue_step(StubLLMstep(fail_with_status=503))
    stub_missing.enqueue_step(StubLLMstep(content="No debería llegar aquí"))
    cfg_stub_no_fb = StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_key=primary_key,
        studio_llm_fallback_key=None,
        studio_stub_llm=True,
    )
    client_stub_no_fb = OpenAICompatibleLLMClient(
        config=cfg_stub_no_fb,
        stub_client=stub_missing,
    )
    with pytest.raises(LLMProviderError, match="STUDIO_LLM_FALLBACK_KEY"):
        await client_stub_no_fb.complete(messages=[{"role": "user", "content": "Hola"}])


@pytest.mark.anyio
async def test_history_window_loads_most_recent_exchanges_in_chronological_order(
    studio_config: StudioConfig,
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    """M2-SCR-006: Con >60 mensajes, carga los intercambios MÁS RECIENTES en orden cronológico."""
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Historia Larga")
    pid = project["id"]

    # Insertamos 35 turnos previos = 70 mensajes (> 60 de límite)
    for turn_idx in range(1, 36):
        store.add_chat_message(
            pid,
            role="user",
            content=f"Mensaje usuario turno {turn_idx:02d}",
        )
        store.add_chat_message(
            pid,
            role="assistant",
            content=f"Respuesta asistente turno {turn_idx:02d}",
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

    await agent.run_turn(
        project_id=pid,
        user_message="Mensaje usuario turno 36 actual",
    )

    sent_messages = stub_llm.calls[-1]
    history_only = sent_messages[1:-1]  # excluye system inicial y user actual
    assert len(history_only) == 60

    history_contents = [m["content"] for m in history_only]
    # Los primeros 5 turnos (10 mensajes) quedaron fuera de la ventana de 60
    assert "Mensaje usuario turno 01" not in history_contents
    assert "Respuesta asistente turno 05" not in history_contents
    # La ventana empieza en el turno 06 y termina en el turno 35 (el más reciente) en orden ASC
    assert history_contents[0] == "Mensaje usuario turno 06"
    assert history_contents[1] == "Respuesta asistente turno 06"
    assert history_contents[-2] == "Mensaje usuario turno 35"
    assert history_contents[-1] == "Respuesta asistente turno 35"


def test_chat_history_redacts_persisted_content_and_tool_calls_scr007(
    client: TestClient,
    studio_config: StudioConfig,
) -> None:
    """M2-SCR-007: POST chat con key sintética -> GET chat devuelve contenido enmascarado."""
    primary_key = studio_config.get_llm_key()
    assert primary_key is not None

    proj = client.post(
        "/studio/api/projects",
        json={"name": "Proyecto Redacción Historial SCR-007"},
    ).json()
    pid = proj["id"]

    # Repro exacta: POST chat cuyo mensaje contiene la key sintética y dispara tool_calls (wan_t2v)
    post_resp = client.post(
        f"/studio/api/projects/{pid}/chat",
        json={
            "message": f"Genera un vídeo wan_t2v usando {primary_key}",
            "stream": False,
        },
    )
    assert post_resp.status_code == 200
    assert primary_key not in post_resp.text
    assert SECRET_PLACEHOLDER in post_resp.text

    # GET /studio/api/projects/{id}/chat jamás debe exponer la key en content ni en tool_calls
    get_resp = client.get(f"/studio/api/projects/{pid}/chat")
    assert get_resp.status_code == 200
    assert primary_key not in get_resp.text
    hist_data = get_resp.json()
    messages = hist_data["messages"]
    assert len(messages) >= 2
    assert SECRET_PLACEHOLDER in messages[0]["content"]
    serialized_tool_calls = json.dumps([m.get("tool_calls") for m in messages], ensure_ascii=False)
    assert primary_key not in serialized_tool_calls
    assert SECRET_PLACEHOLDER in serialized_tool_calls

