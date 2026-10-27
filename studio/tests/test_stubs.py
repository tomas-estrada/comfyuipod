"""Tests para los dobles in-process de ComfyUI y LLM (STUDIO_STUB_LLM)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app
from comfy_studio.stubs.llm import StubLLMClient


def test_comfyui_stub_endpoints_lifecycle() -> None:
    state = ComfyUIStubState(healthy=True)
    stub_app = create_stub_comfyui_app(state)
    with TestClient(stub_app) as c:
        stats = c.get("/system_stats")
        assert stats.status_code == 200
        assert "devices" in stats.json()

        submit = c.post(
            "/prompt",
            json={"prompt": {"1": {"class_type": "KSampler", "inputs": {}}}},
        )
        assert submit.status_code == 200
        prompt_id = submit.json()["prompt_id"]
        assert prompt_id

        hist = c.get(f"/history/{prompt_id}")
        assert hist.status_code == 200
        assert prompt_id in hist.json()
        assert hist.json()[prompt_id]["status"]["status_str"] == "success"

        queue = c.get("/queue")
        assert queue.status_code == 200
        assert "queue_running" in queue.json()

        up = c.post(
            "/upload/image",
            files={"image": ("frame.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
        assert up.status_code == 200
        assert up.json()["name"] == "frame.png"

        view = c.get("/view?filename=frame.png&type=input")
        assert view.status_code == 200
        assert view.content.startswith(b"\x89PNG")


def test_stub_llm_client_returns_deterministic_reply() -> None:
    llm = StubLLMClient()
    reply = llm.chat([{"role": "user", "content": "Genera un clip de 5s"}])
    assert reply["role"] == "assistant"
    assert reply["stub"] is True
    assert "content" in reply
