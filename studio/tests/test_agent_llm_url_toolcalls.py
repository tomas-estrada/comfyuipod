"""Tests de URL de endpoint, guard SSRF en la entrada del cliente y parsing de tool-calls.

Cubre sin GPU, sin red real y sin llamadas LLM:
- `build_chat_completions_url`: las tres ramas de construcción del endpoint.
- Guard SSRF en el borde del cliente (regla AGENTS.md 5): `complete` y
  `stream_complete` rechazan imágenes por URL interna ANTES de tocar el transporte.
- `_parse_tool_calls` y `LLMToolCall.to_openai_dict`: JSON malformado, campos
  ausentes y formas degeneradas de `arguments`.
- Parsing de respuestas HTTP/SSE en `_http_complete` / `_http_stream_complete`.
- No-fallback cuando ya se emitieron tokens a mitad de stream.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from comfy_studio.agent.llm import (
    LLMProviderError,
    LLMToolCall,
    OpenAICompatibleLLMClient,
    _parse_tool_calls,
    build_chat_completions_url,
)
from comfy_studio.config import StudioConfig

_PRIMARY_BASE = "https://llm-primary.test/v1"
_FALLBACK_BASE = "https://llm-fallback.test/v1"
_PRIMARY_ENDPOINT = "https://llm-primary.test/v1/chat/completions"
_FALLBACK_ENDPOINT = "https://llm-fallback.test/v1/chat/completions"
_PRIMARY_KEY = "YOUR_STUDIO_API_KEY_HERE"
_FALLBACK_KEY = "YOUR_STUDIO_FALLBACK_KEY_HERE"
_DATA_URL = "data:image/png;base64,aGVsbG8="
_MISSING: Any = object()


def _config(studio_data_dir: Path) -> StudioConfig:
    """Config HTTP aislada (sin stub) con primario y fallback en hosts distintos."""
    return StudioConfig(
        studio_data_dir=studio_data_dir,
        studio_llm_base_url=_PRIMARY_BASE,
        studio_llm_model="cfg-model",
        studio_llm_key=_PRIMARY_KEY,
        studio_llm_fallback_base_url=_FALLBACK_BASE,
        studio_llm_fallback_model="cfg-fallback-model",
        studio_llm_fallback_key=_FALLBACK_KEY,
        studio_stub_llm=False,
    )


def _client(studio_data_dir: Path, handler: Any) -> OpenAICompatibleLLMClient:
    return OpenAICompatibleLLMClient(
        config=_config(studio_data_dir),
        transport=httpx.MockTransport(handler),
    )


def _sse_frame(obj: Any) -> bytes:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


def _message_response(
    content: Any,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
    model: str = "remote-model",
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason},
        ],
    }


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("https://api.example.com/v1", "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com", "https://api.example.com/v1/chat/completions"),
        ("https://api.example.com/v1/", "https://api.example.com/v1/chat/completions"),
        (
            "https://api.example.com/v1/chat/completions",
            "https://api.example.com/v1/chat/completions",
        ),
        (
            "https://api.example.com/v1/chat/completions/",
            "https://api.example.com/v1/chat/completions",
        ),
        ("  https://api.example.com/v1  ", "https://api.example.com/v1/chat/completions"),
        (
            "https://api.example.com/openai",
            "https://api.example.com/openai/v1/chat/completions",
        ),
    ],
)
def test_build_chat_completions_url(base: str, expected: str) -> None:
    """Ramas: ya-canónico, con `/v1`, sin prefijo, slash final y espacios."""
    assert build_chat_completions_url(base) == expected


@pytest.mark.anyio
@pytest.mark.parametrize("method", ["complete", "stream_complete"])
@pytest.mark.parametrize(
    "bad_image_url",
    [
        "http://127.0.0.1:8188/panel/secret.png",
        "//169.254.169.254/latest/meta-data.png",
        "https://studio-internal.local/keyframe.png",
    ],
)
async def test_client_entry_rejects_internal_urls_before_any_request(
    studio_data_dir: Path,
    method: str,
    bad_image_url: str,
) -> None:
    """Regla AGENTS.md 5: la validación corre ANTES de cualquier I/O de red."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500, json={"error": "no debería llamarse"}, request=request)

    client = _client(studio_data_dir, handler)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe esta imagen"},
                {"type": "image_url", "image_url": {"url": bad_image_url}},
            ],
        }
    ]
    with pytest.raises(ValueError):
        if method == "complete":
            await client.complete(messages)
        else:
            await client.stream_complete(messages)
    assert calls == []


@pytest.mark.anyio
async def test_complete_sends_inline_data_url_to_built_endpoint(studio_data_dir: Path) -> None:
    """Con data URL legítimo pasa el guard, usa el endpoint construido y parsea la respuesta."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append((str(request.url), payload))
        return httpx.Response(200, json=_message_response("listo"), request=request)

    client = _client(studio_data_dir, handler)
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": _DATA_URL}},
            ],
        }
    ]
    resp = await client.complete(messages)

    assert seen and seen[0][0] == _PRIMARY_ENDPOINT
    sent_parts = seen[0][1]["messages"][0]["content"]
    assert sent_parts[1]["image_url"]["url"] == _DATA_URL
    assert resp.content == "listo"
    assert resp.model == "remote-model"
    assert resp.provider == "primary"
    assert resp.finish_reason == "stop"


@pytest.mark.anyio
async def test_complete_accepts_string_form_data_url_image(studio_data_dir: Path) -> None:
    """`image_url` como string plano con data URL válido pasa el guard del cliente."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_message_response(""), request=request)

    client = _client(studio_data_dir, handler)
    messages = [{"role": "user", "content": [{"type": "image_url", "image_url": _DATA_URL}]}]
    resp = await client.complete(messages)
    assert calls == [_PRIMARY_ENDPOINT]
    assert resp.content == ""


def test_parse_tool_calls_ignores_empty_and_non_dict_entries() -> None:
    """Entradas vacías, no-dict o sin nombre válido no producen llamadas."""
    assert _parse_tool_calls(None) == []
    assert _parse_tool_calls([]) == []
    assert _parse_tool_calls(["no-dict", 42, None]) == []
    assert _parse_tool_calls([{"id": "1"}]) == []
    assert _parse_tool_calls([{"id": "2", "function": {"name": "   "}}]) == []


def test_parse_tool_calls_generates_missing_ids_and_top_level_name() -> None:
    """Sin `id` se genera `call_<hex10>_<idx>`; sin `function` se usa el nombre superior."""
    calls = _parse_tool_calls(
        [
            "basura",
            {"function": {"name": "search_assets"}},
            {"name": "render"},
        ]
    )
    assert [tc.name for tc in calls] == ["search_assets", "render"]
    # El índice enumerado (incluye las entradas descartadas) alimenta el id generado.
    assert re.fullmatch(r"call_[0-9a-f]{10}_1", calls[0].id)
    assert re.fullmatch(r"call_[0-9a-f]{10}_2", calls[1].id)


@pytest.mark.parametrize(
    ("raw_args", "expected_arguments", "expected_raw"),
    [
        ({"q": "x"}, {"q": "x"}, '{"q": "x"}'),
        ('{"q": "x"}', {"q": "x"}, '{"q": "x"}'),
        ("5", {"_value": 5}, "5"),
        ('"texto"', {"_value": "texto"}, '"texto"'),
        ("[1, 2]", {"_value": [1, 2]}, "[1, 2]"),
        ("{roto", {"_raw": "{roto"}, "{roto"),
        (_MISSING, {}, "{}"),
        (None, {}, "{}"),
        ("", {}, "{}"),
        (0, {}, "{}"),
        (7, {"_value": 7}, "7"),
        (True, {"_raw": "True"}, "True"),
    ],
)
def test_parse_tool_calls_argument_shapes(
    raw_args: Any,
    expected_arguments: dict[str, Any],
    expected_raw: str,
) -> None:
    """Formas de `arguments`: dict, JSON válido/inválido, no-objeto, ausente y nulo."""
    if raw_args is _MISSING:
        entry: dict[str, Any] = {"id": "t1", "function": {"name": "tool"}}
    else:
        entry = {"id": "t1", "function": {"name": "tool", "arguments": raw_args}}
    calls = _parse_tool_calls([entry])
    assert len(calls) == 1
    assert calls[0].arguments == expected_arguments
    assert calls[0].raw_arguments == expected_raw


def test_parse_tool_calls_top_level_arguments_fallback() -> None:
    """`function` sin clave `arguments` cae al `arguments` de nivel superior."""
    calls = _parse_tool_calls([{"id": "t1", "function": {"name": "tool"}, "arguments": {"a": 1}}])
    assert len(calls) == 1
    assert calls[0].arguments == {"a": 1}
    assert calls[0].raw_arguments == '{"a": 1}'


def test_parse_tool_calls_null_function_arguments_beat_top_level() -> None:
    """`function.arguments: null` gana sobre el nivel superior (clave presente = valor None)."""
    calls = _parse_tool_calls(
        [
            {
                "id": "t1",
                "function": {"name": "tool", "arguments": None},
                "arguments": {"a": 1},
            }
        ]
    )
    assert len(calls) == 1
    assert calls[0].arguments == {}
    assert calls[0].raw_arguments == "{}"


def test_to_openai_dict_prefers_raw_arguments() -> None:
    """Con `raw_arguments` poblado se reenvía el texto crudo tal cual."""
    call = LLMToolCall(
        id="c1",
        name="tool",
        arguments={"q": "x"},
        raw_arguments='{"q":"x"}',
    )
    assert call.to_openai_dict() == {
        "id": "c1",
        "type": "function",
        "function": {"name": "tool", "arguments": '{"q":"x"}'},
    }


def test_to_openai_dict_dumps_arguments_when_raw_empty() -> None:
    """Con `raw_arguments` vacío se serializa `arguments` con `json.dumps`."""
    call = LLMToolCall(id="c2", name="tool", arguments={"q": "x"}, raw_arguments="")
    assert call.to_openai_dict()["function"]["arguments"] == '{"q": "x"}'


@pytest.mark.anyio
async def test_complete_parses_tool_calls_list_content_and_finish_reason(
    studio_data_dir: Path,
) -> None:
    """Respuesta con content-list, tool-calls mixtas (string/dict) y finish_reason."""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = _message_response(
            [
                {"type": "text", "text": "mirando "},
                {"type": "text", "text": "assets"},
            ],
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search_assets", "arguments": '{"q": "gorra"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "enqueue_render", "arguments": {"template_id": "wan"}},
                },
            ],
            finish_reason="tool_calls",
        )
        return httpx.Response(200, json=body, request=request)

    client = _client(studio_data_dir, handler)
    resp = await client.complete([{"role": "user", "content": "hola"}])

    assert resp.content == "mirando assets"
    assert resp.finish_reason == "tool_calls"
    assert resp.model == "remote-model"
    assert resp.provider == "primary"
    assert [tc.name for tc in resp.tool_calls] == ["search_assets", "enqueue_render"]
    assert resp.tool_calls[0].arguments == {"q": "gorra"}
    assert resp.tool_calls[0].raw_arguments == '{"q": "gorra"}'
    assert resp.tool_calls[1].arguments == {"template_id": "wan"}


@pytest.mark.anyio
async def test_complete_with_empty_choices_uses_defaults(studio_data_dir: Path) -> None:
    """`choices: []` → content vacío, finish `stop` y modelo pedido como respaldo."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"model": "", "choices": []}, request=request)

    client = _client(studio_data_dir, handler)
    resp = await client.complete([{"role": "user", "content": "hola"}])
    assert calls == [_PRIMARY_ENDPOINT]
    assert resp.content == ""
    assert resp.tool_calls == []
    assert resp.finish_reason == "stop"
    assert resp.model == "cfg-model"


@pytest.mark.anyio
async def test_complete_fails_closed_on_malformed_json_body(studio_data_dir: Path) -> None:
    """JSON inválido en la respuesta: fallan ambos proveedores (LLMProviderError)."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            content=b"{esto no es json",
            headers={"content-type": "application/json"},
            request=request,
        )

    client = _client(studio_data_dir, handler)
    with pytest.raises(LLMProviderError):
        await client.complete([{"role": "user", "content": "hola"}])
    assert calls == [_PRIMARY_ENDPOINT, _FALLBACK_ENDPOINT]


@pytest.mark.anyio
async def test_complete_fails_closed_on_malformed_function_shape(studio_data_dir: Path) -> None:
    """`function` no-dict en el payload: sin tool-calls parciales, falla en ambos."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        body = _message_response(
            "x",
            tool_calls=[{"id": "1", "function": "oops"}],
            finish_reason="tool_calls",
        )
        return httpx.Response(200, json=body, request=request)

    client = _client(studio_data_dir, handler)
    with pytest.raises(LLMProviderError):
        await client.complete([{"role": "user", "content": "hola"}])
    assert calls == [_PRIMARY_ENDPOINT, _FALLBACK_ENDPOINT]


@pytest.mark.anyio
async def test_stream_sse_accumulates_tool_calls_across_chunks(studio_data_dir: Path) -> None:
    """SSE multi-chunk: acumula id/nombre/argumentos por índice y salta líneas basura."""
    frames: list[dict[str, Any]] = [
        {
            "id": "c1",
            "model": "remote-sse",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "Voy a "},
                    "finish_reason": None,
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                # sin "index": cae a la posición dentro de la lista (0)
                                "id": "call_abc123def4",
                                "type": "function",
                                "function": {"name": "search_", "arguments": ""},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "assets", "arguments": '{"q":'},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"arguments": '"gorra"}'}}
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call_zz99",
                                "function": {
                                    "name": "render",
                                    "arguments": {"template_id": "wan"},
                                },
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
    ]
    junk = b"".join(
        [
            b"event: message\n",
            b": keep-alive\n",
            b"data: {esto no es json\n",
            b"data: [1, 2, 3]\n\n",
            b"data:\n\n",
            b"linea-sin-data\n\n",
            _sse_frame({"choices": []}),
            _sse_frame({"choices": [{"index": 0, "delta": "oops"}]}),
        ]
    )
    body = b"".join(_sse_frame(frame) for frame in frames) + junk + b"data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            request=request,
        )

    client = _client(studio_data_dir, handler)
    received: list[tuple[str, str]] = []

    def on_token(piece: str, model: str) -> None:
        received.append((piece, model))

    resp = await client.stream_complete(
        [{"role": "user", "content": "hola"}],
        on_token=on_token,
    )

    assert resp.content == "Voy a "
    assert resp.model == "remote-sse"
    assert resp.finish_reason == "tool_calls"
    assert resp.provider == "primary"
    assert [tc.name for tc in resp.tool_calls] == ["search_assets", "render"]
    assert resp.tool_calls[0].id == "call_abc123def4"
    assert resp.tool_calls[0].arguments == {"q": "gorra"}
    assert resp.tool_calls[1].id == "call_zz99"
    assert resp.tool_calls[1].arguments == {"template_id": "wan"}
    assert received == [("Voy a ", "remote-sse")]


@pytest.mark.anyio
async def test_stream_sse_falls_back_to_message_and_joins_list_content(
    studio_data_dir: Path,
) -> None:
    """Chunk sin `delta` usa `message`; content-list se une antes de emitir tokens."""
    frame = {
        "id": "c2",
        "model": "remote-sse",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "pieza uno "},
                        {"type": "text", "text": "pieza dos"},
                    ],
                },
                "finish_reason": "stop",
            }
        ],
    }
    body = _sse_frame(frame) + b"data: [DONE]\n\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            request=request,
        )

    client = _client(studio_data_dir, handler)
    received: list[tuple[str, str]] = []
    resp = await client.stream_complete(
        [{"role": "user", "content": "hola"}],
        on_token=lambda piece, model: received.append((piece, model)),
    )
    assert resp.content == "pieza uno pieza dos"
    assert resp.finish_reason == "stop"
    assert resp.model == "remote-sse"
    assert resp.tool_calls == []
    # El redactor retiene el sufijo final "s" (podría ser el inicio de "sk-…") y lo
    # entrega en flush: las piezas emitidas suman exactamente el content completo.
    assert "".join(piece for piece, _ in received) == "pieza uno pieza dos"
    assert "pieza uno pieza dos".startswith(received[0][0])
    assert all(model == "remote-sse" for _, model in received)


@pytest.mark.anyio
async def test_stream_non_sse_json_replays_content_in_word_chunks(studio_data_dir: Path) -> None:
    """JSON a stream=True: parsea el body y reparte content con `_iter_text_chunks`."""
    body = _message_response(
        "hola mundo desde el pod",
        tool_calls=[
            {
                "id": "call_n1",
                "type": "function",
                "function": {"name": "tool", "arguments": '{"a": 1}'},
            }
        ],
        finish_reason="tool_calls",
        model="remote-json",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    client = _client(studio_data_dir, handler)
    received: list[tuple[str, str]] = []
    resp = await client.stream_complete(
        [{"role": "user", "content": "hola"}],
        on_token=lambda piece, model: received.append((piece, model)),
    )
    assert resp.content == "hola mundo desde el pod"
    assert resp.model == "remote-json"
    assert resp.finish_reason == "tool_calls"
    assert [tc.name for tc in resp.tool_calls] == ["tool"]
    assert resp.tool_calls[0].arguments == {"a": 1}
    assert [piece for piece, _ in received] == ["hola ", "mundo ", "desde ", "el ", "pod"]
    assert all(model == "remote-json" for _, model in received)


@pytest.mark.anyio
async def test_stream_fails_closed_on_malformed_json_body(studio_data_dir: Path) -> None:
    """Body JSON inválido con stream=True: fallan ambos proveedores (LLMProviderError)."""
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            content=b"{roto",
            headers={"content-type": "application/json"},
            request=request,
        )

    client = _client(studio_data_dir, handler)
    with pytest.raises(LLMProviderError):
        await client.stream_complete([{"role": "user", "content": "hola"}])
    assert calls == [_PRIMARY_ENDPOINT, _FALLBACK_ENDPOINT]


class _TokenThenFailStream(httpx.AsyncByteStream):
    """Emite un frame SSE completo y luego falla la lectura (corte a mitad de stream)."""

    def __init__(self, first_frame: bytes) -> None:
        self._first = first_frame

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        raise httpx.ReadError("lectura interrumpida tras el primer token")


@pytest.mark.anyio
async def test_stream_midstream_failure_after_token_skips_fallback(studio_data_dir: Path) -> None:
    """Tras emitir tokens, una falla de red NO reintenta el fallback (evita duplicar texto)."""
    frame = {
        "id": "c9",
        "model": "remote-sse",
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": "hola "},
                "finish_reason": None,
            }
        ],
    }
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=_TokenThenFailStream(_sse_frame(frame)),
            request=request,
        )

    client = _client(studio_data_dir, handler)
    received: list[str] = []

    def on_token(piece: str, model: str) -> None:
        received.append(piece)

    with pytest.raises(LLMProviderError):
        await client.stream_complete(
            [{"role": "user", "content": "hola"}],
            on_token=on_token,
        )
    # El token sí se entregó antes de la falla…
    assert received == ["hola "]
    # …y por eso el fallback NUNCA se intentó.
    assert calls == [_PRIMARY_ENDPOINT]
