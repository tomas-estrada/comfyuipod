"""Deterministic, simulated contract-server coverage for A06 provider behavior."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from comfy_studio.agent.llm import LLMProviderError, OpenAICompatibleLLMClient
from comfy_studio.app import create_app
from comfy_studio.config import StudioConfig

PRIMARY = "https://primary.contract.test/v1/chat/completions"
FALLBACK = "https://fallback.contract.test/v1/chat/completions"
PRIMARY_SENTINEL = "contract-primary-sentinel-7fd2"
FALLBACK_SENTINEL = "contract-fallback-sentinel-881a"
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


@dataclass
class ContractExchange:
    """A local provider fixture; never evidence of real API connectivity."""

    request: httpx.Request
    simulated: bool = True


def _config(data_dir: Path) -> StudioConfig:
    return StudioConfig(
        studio_data_dir=data_dir,
        studio_stub_llm=False,
        studio_llm_base_url="https://primary.contract.test/v1",
        studio_llm_model="primary-contract-v3",
        studio_llm_key=PRIMARY_SENTINEL,
        studio_llm_supports_tools=True,
        studio_llm_supports_streaming=True,
        studio_llm_fallback_base_url="https://fallback.contract.test/v1",
        studio_llm_fallback_model="fallback-contract-v8",
        studio_llm_fallback_key=FALLBACK_SENTINEL,
        studio_llm_fallback_supports_tools=False,
        studio_llm_fallback_supports_streaming=False,
    )


def _response(content: str = "ok") -> dict[str, Any]:
    return {
        "model": "contract-response",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
    }


@pytest.mark.anyio
async def test_provider_profiles_send_distinct_models_and_capabilities(
    studio_data_dir: Path,
) -> None:
    seen: list[ContractExchange] = []

    async def server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        body = json.loads(request.content)
        if request.url.host == "primary.contract.test":
            assert body["model"] == "primary-contract-v3"
            assert body["stream"] is True
            assert body["tools"] == TOOLS
            return httpx.Response(503, json={"error": "try fallback"}, request=request)
        assert body["model"] == "fallback-contract-v8"
        assert body["stream"] is False
        assert "tools" not in body
        return httpx.Response(200, json=_response("fallback ok"), request=request)

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(server)
    )
    result = await client.stream_complete([{"role": "user", "content": "hi"}], tools=TOOLS)

    assert result.provider == "fallback"
    assert result.model == "contract-response"
    assert result.content == "fallback ok"
    assert len(seen) == 2 and all(exchange.simulated for exchange in seen)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "body", "kind", "retryable"),
    [
        (401, {"error": PRIMARY_SENTINEL}, "authentication", False),
        (429, {"error": "rate limited"}, "rate_limit", True),
        (400, {"error": {"code": "context_length_exceeded"}}, "context_limit", False),
    ],
)
async def test_contract_http_errors_are_typed_bounded_and_redacted(
    studio_data_dir: Path,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: dict[str, Any],
    kind: str,
    retryable: bool,
) -> None:
    seen: list[ContractExchange] = []

    async def server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        return httpx.Response(status, json=body, request=request)

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(server)
    )
    with pytest.raises(LLMProviderError) as caught:
        await client.complete([{"role": "user", "content": "brief"}])

    error = caught.value
    assert error.kind == kind
    assert error.status_code == status
    assert error.retryable is retryable
    assert PRIMARY_SENTINEL not in str(error) and FALLBACK_SENTINEL not in str(error)
    assert PRIMARY_SENTINEL not in caplog.text and FALLBACK_SENTINEL not in caplog.text
    assert len(seen) == 2 and all(exchange.simulated for exchange in seen)


@pytest.mark.anyio
async def test_timeout_is_retryable_and_both_attempts_are_bounded(studio_data_dir: Path) -> None:
    seen: list[ContractExchange] = []

    async def server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        raise httpx.ReadTimeout(f"timeout token={PRIMARY_SENTINEL}", request=request)

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(server)
    )
    with pytest.raises(LLMProviderError) as caught:
        await client.complete([{"role": "user", "content": "brief"}])
    assert caught.value.kind == "timeout"
    assert caught.value.retryable is True
    assert PRIMARY_SENTINEL not in str(caught.value)
    assert len(seen) == 2 and all(exchange.simulated for exchange in seen)


@pytest.mark.anyio
async def test_invalid_json_and_valid_no_tool_response_are_distinguished(
    studio_data_dir: Path,
) -> None:
    seen: list[ContractExchange] = []

    async def server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        return httpx.Response(200, content=b"{not-json", request=request)

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(server)
    )
    with pytest.raises(LLMProviderError) as caught:
        await client.complete([{"role": "user", "content": "bad response"}])
    assert caught.value.kind == "invalid_json"
    assert len(seen) == 2

    async def no_tool_server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        return httpx.Response(200, json=_response("text only"), request=request)

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(no_tool_server)
    )
    response = await client.complete([{"role": "user", "content": "no tool call"}], tools=TOOLS)
    assert response.content == "text only" and response.tool_calls == []
    assert all(exchange.simulated for exchange in seen)


@pytest.mark.anyio
async def test_stream_callback_redacts_sentinel_split_across_sse_frames(
    studio_data_dir: Path,
) -> None:
    seen: list[ContractExchange] = []
    pieces = [PRIMARY_SENTINEL[:15], PRIMARY_SENTINEL[15:]]

    async def server(request: httpx.Request) -> httpx.Response:
        seen.append(ContractExchange(request))
        frames = (
            b"".join(
                (
                    "data: "
                    + json.dumps(
                        {"model": "contract-stream", "choices": [{"delta": {"content": p}}]}
                    )
                    + "\n\n"
                ).encode()
                for p in pieces
            )
            + b"data: [DONE]\n\n"
        )
        return httpx.Response(
            200, content=frames, headers={"content-type": "text/event-stream"}, request=request
        )

    client = OpenAICompatibleLLMClient(
        _config(studio_data_dir), transport=httpx.MockTransport(server)
    )
    tokens: list[str] = []

    async def on_token(token: str, _model: str) -> None:
        tokens.append(token)

    response = await client.stream_complete(
        [{"role": "user", "content": "stream"}], on_token=on_token
    )
    public_text = response.content + "".join(tokens)
    assert PRIMARY_SENTINEL not in public_text
    assert "[REDACTED]" in public_text
    assert len(seen) == 1 and seen[0].simulated is True


def test_secret_is_redacted_from_chat_json_sse_and_persisted_history(studio_data_dir: Path) -> None:
    config = _config(studio_data_dir)
    app = create_app(config)

    async def server(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_response(f"fixture said {PRIMARY_SENTINEL}"), request=request
        )

    app.state.agent._llm_client = OpenAICompatibleLLMClient(
        config, transport=httpx.MockTransport(server)
    )
    with TestClient(app) as client:
        project_id = client.post("/studio/api/projects", json={"name": "contract"}).json()["id"]
        for stream in (False, True):
            response = client.post(
                f"/studio/api/projects/{project_id}/chat",
                json={"message": "hello", "stream": stream},
            )
            assert response.status_code == 200
            assert PRIMARY_SENTINEL not in response.text
            assert "[REDACTED]" in response.text
            if not stream:
                run_id = response.json()["run_id"]
                checkpoint = app.state.store.get_meta(project_id, f"agent_run:{run_id}")
                assert PRIMARY_SENTINEL not in str(checkpoint)
                assert "reply_sha256" in str(checkpoint)
        history = client.get(f"/studio/api/projects/{project_id}/chat")
        assert PRIMARY_SENTINEL not in history.text
        assert "[REDACTED]" in history.text
