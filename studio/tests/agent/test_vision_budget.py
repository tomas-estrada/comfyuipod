"""Invariante: `vision_check` cuenta su llamada LLM contra `CallBudget`.

Causa (pre-fix): el loop consume el presupuesto por iteración
(`CallBudget.consume()`), pero `_handle_vision_check` crea su propio
`OpenAICompatibleLLMClient` y llama a `complete()` sin tocar el
presupuesto. Con `max_llm_calls_per_turn=1`, el primer `stream_complete`
del loop consumía la única llamada y `vision_check` gastaba una segunda
llamada no contabilizada.

El test usa un servidor HTTP local real en loopback que cuenta las
peticiones al proveedor (no un mock que finge un proveedor sano): con
presupuesto 1, el turno debe hacer una sola petición al proveedor.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from comfy_studio.agent.llm import OpenAICompatibleLLMClient
from comfy_studio.agent.loop import StudioAgent
from comfy_studio.agent.tools import create_default_tool_belt
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"


class _CountingHandler(BaseHTTPRequestHandler):
    # Configurado por el test (clase padre no debe compartir estado entre runs).
    asset_id: str = ""
    hits: list[dict[str, Any]] = []

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - firma stdlib
        return

    def do_POST(self) -> None:  # noqa: N802 - firma stdlib
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        payload = json.loads(raw.decode("utf-8") or "{}")
        type(self).hits.append(payload)
        if not self.path.endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":{"message":"not found"}}')
            return
        if payload.get("stream"):
            tools = payload.get("tools") or []
            if tools:
                delta = {
                    "id": "cmpl-test",
                    "model": "test-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_vision_1",
                                        "type": "function",
                                        "function": {
                                            "name": "vision_check",
                                            "arguments": json.dumps(
                                                {
                                                    "asset_id": type(self).asset_id,
                                                    "question": "¿Se ve bien?",
                                                },
                                                ensure_ascii=False,
                                            ),
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                body = (
                    f"data: {json.dumps(delta, ensure_ascii=False)}\n\ndata: [DONE]\n\n"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                return
            delta = {
                "id": "cmpl-test",
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Listo."},
                        "finish_reason": "stop",
                    }
                ],
            }
            body = (f"data: {json.dumps(delta, ensure_ascii=False)}\n\ndata: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        data = {
            "id": "cmpl-test",
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Se ve bien"},
                    "finish_reason": "stop",
                }
            ],
        }
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


def _start_server(asset_id: str) -> tuple[ThreadingHTTPServer, threading.Thread]:
    _CountingHandler.asset_id = asset_id
    _CountingHandler.hits = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.anyio
async def test_vision_check_counts_against_call_budget(
    studio_data_dir: Path,
    stub_comfyui_transport: httpx.ASGITransport,
) -> None:
    store = ProjectStore(studio_data_dir)
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto vision-budget")
    asset = store.save_upload(
        project["id"],
        filename="frame.png",
        content_type="image/png",
        data=PNG_BYTES,
        max_upload_bytes=1024 * 1024,
        category="uploads",
    )
    server, thread = _start_server(str(asset["id"]))
    try:
        port = server.server_address[1]
        cfg = StudioConfig(
            studio_data_dir=studio_data_dir,
            studio_llm_base_url=f"http://127.0.0.1:{port}/v1",
            studio_llm_model="test-model",
            studio_llm_key="test-primary-key-for-vision-budget",
            studio_llm_fallback_base_url=f"http://127.0.0.1:{port}/v1",
            studio_llm_fallback_model="test-model",
            studio_llm_fallback_key="test-fallback-key-for-vision-budget",
            studio_stub_llm=False,
        )
        comfy_client = ComfyUIClient(
            base_url="http://127.0.0.1:9",
            transport=stub_comfyui_transport,
            store=store,
            broker=broker,
            redactor=cfg.redactor(),
        )
        agent = StudioAgent(
            config=cfg,
            store=store,
            comfyui_client=comfy_client,
            broker=broker,
            llm_client=OpenAICompatibleLLMClient(config=cfg, timeout=5.0),
            tool_belt=create_default_tool_belt(),
            max_llm_calls_per_turn=1,
        )

        result = await agent.run_turn(project_id=project["id"], user_message="inspecciona el frame")

        # Con presupuesto 1, el turno agota la llamada en la primera
        # iteración del loop; vision_check no debe gastar otra.
        assert result.stop_reason == "call_budget_exhausted"
        tool_results = [ev for ev in result.events if ev["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["name"] == "vision_check"
        assert tool_results[0]["is_error"] is True
        assert "presupuesto" in str(tool_results[0]["result"]).lower()
        assert len(_CountingHandler.hits) == 1
        assert _CountingHandler.hits[0].get("stream") is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
