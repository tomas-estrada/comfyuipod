"""Cliente LLM compatible-OpenAI (DashScope primario + fallback DeepSeek + visión base64)."""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

from ..config import RedactingLogFilter, SecretRedactor, StudioConfig
from ..stubs.llm import StubLLMClient

logger = logging.getLogger("comfy_studio.agent.llm")

ALLOWED_IMAGE_MIMES = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp"})


class LLMProviderError(RuntimeError):
    """Error al invocar el proveedor LLM primario y su fallback secundario."""


@dataclass(slots=True)
class LLMToolCall:
    """Llamada a herramienta devuelta por el modelo compatible-OpenAI."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: str = "{}"

    def to_openai_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments
                if self.raw_arguments
                else json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class LLMResponse:
    """Respuesta normalizada de una invocación al cliente LLM."""

    content: str
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    model: str = ""
    provider: Literal["primary", "fallback", "stub"] = "primary"
    finish_reason: str = "stop"


def encode_image_to_data_url(data: bytes, mime_type: str = "image/png") -> str:
    """Codifica bytes de imagen como data URL en base64 para visión del LLM."""
    if not isinstance(data, (bytes, bytearray)) or len(data) == 0:
        raise ValueError("Los bytes de imagen para base64 no pueden estar vacíos.")
    normalized_mime = (mime_type or "image/png").split(";")[0].strip().lower()
    if normalized_mime == "image/jpg":
        normalized_mime = "image/jpeg"
    if normalized_mime not in ALLOWED_IMAGE_MIMES:
        raise ValueError(f"MIME de imagen no soportado para visión LLM: {mime_type!r}")
    b64_str = base64.b64encode(bytes(data)).decode("ascii")
    return f"data:{normalized_mime};base64,{b64_str}"


def build_vision_content_part(
    image_bytes: bytes,
    mime_type: str = "image/png",
    *,
    detail: str = "auto",
) -> dict[str, Any]:
    """Construye un bloque `image_url` compatible-OpenAI con imagen inline en base64."""
    return {
        "type": "image_url",
        "image_url": {
            "url": encode_image_to_data_url(image_bytes, mime_type),
            "detail": detail,
        },
    }


def validate_DataUrl_only(url_value: str) -> None:
    """Garantiza que una URL de imagen para el LLM sea estrictamente data:image/...;base64,."""
    cleaned = (url_value or "").strip()
    if not cleaned.startswith("data:image/") or ";base64," not in cleaned:
        raise ValueError(
            "Las imágenes/keyframes para el LLM deben ir en base64 (data:image/...;base64,...); "
            "nunca se permiten URLs HTTP/HTTPS ni URLs con autenticación del pod."
        )


def validate_no_auth_urls_in_messages(messages: Sequence[dict[str, Any]]) -> None:
    """Verifica que ningún mensaje contenga imágenes por URL HTTP/HTTPS o con credenciales."""
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    image_url_obj = part.get("image_url")
                    url_val = (
                        image_url_obj.get("url", "")
                        if isinstance(image_url_obj, dict)
                        else str(image_url_obj or "")
                    )
                    validate_DataUrl_only(url_val)


def build_chat_completions_url(base_url: str) -> str:
    """Construye el endpoint `/chat/completions` para un `base_url` compatible-OpenAI."""
    cleaned = base_url.strip().rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"
    return f"{cleaned}/v1/chat/completions"


def _parse_tool_calls(raw_tool_calls: Sequence[dict[str, Any]] | None) -> list[LLMToolCall]:
    if not raw_tool_calls:
        return []
    parsed: list[LLMToolCall] = []
    for idx, raw in enumerate(raw_tool_calls):
        if not isinstance(raw, dict):
            continue
        call_id = str(raw.get("id") or f"call_{uuid.uuid4().hex[:10]}_{idx}")
        fn = raw.get("function") or {}
        name = str(fn.get("name") or raw.get("name") or "").strip()
        raw_args = fn.get("arguments", raw.get("arguments", "{}"))
        if isinstance(raw_args, dict):
            args_dict = raw_args
            raw_args_str = json.dumps(raw_args, ensure_ascii=False)
        else:
            raw_args_str = str(raw_args or "{}")
            try:
                loaded = json.loads(raw_args_str)
                args_dict = loaded if isinstance(loaded, dict) else {"_value": loaded}
            except json.JSONDecodeError:
                args_dict = {"_raw": raw_args_str}
        if name:
            parsed.append(
                LLMToolCall(
                    id=call_id,
                    name=name,
                    arguments=args_dict,
                    raw_arguments=raw_args_str,
                )
            )
    return parsed


class OpenAICompatibleLLMClient:
    """Cliente compatible-OpenAI con primario DashScope intl y fallback DeepSeek."""

    def __init__(
        self,
        config: StudioConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        stub_client: StubLLMClient | None = None,
        timeout: float = 30.0,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout = timeout
        self._redactor = redactor or config.redactor()
        self._stub_client = stub_client or (
            StubLLMClient(model=config.studio_llm_model)
            if config.studio_stub_llm and transport is None
            else None
        )

        for logger_name in ("comfy_studio", "comfy_studio.agent", "comfy_studio.agent.llm"):
            target_logger = logging.getLogger(logger_name)
            if not any(isinstance(f, RedactingLogFilter) for f in target_logger.filters):
                target_logger.addFilter(RedactingLogFilter(self._redactor))

    @property
    def stub_client(self) -> StubLLMClient | None:
        return self._stub_client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = self._config.get_llm_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Ejecuta una llamada chat/completions con fallback automático al proveedor secundario."""
        validate_no_auth_urls_in_messages(messages)

        if self._stub_client is not None and self._transport is None:
            return self._complete_with_stub(messages, tools=tools)

        primary_err: Exception | None = None
        try:
            return await self._http_complete(
                base_url=self._config.studio_llm_base_url,
                model=self._config.studio_llm_model,
                provider="primary",
                messages=messages,
                tools=tools,
                temperature=temperature,
            )
        except Exception as exc:
            primary_err = exc
            safe_reason = self._redactor.redact(str(exc))
            logger.warning(
                "Proveedor LLM primario (%s / %s) falló (%s). "
                "Activando fallback secundario (%s / %s).",
                self._config.studio_llm_base_url,
                self._config.studio_llm_model,
                safe_reason,
                self._config.studio_llm_fallback_base_url,
                self._config.studio_llm_fallback_model,
            )

        try:
            return await self._http_complete(
                base_url=self._config.studio_llm_fallback_base_url,
                model=self._config.studio_llm_fallback_model,
                provider="fallback",
                messages=messages,
                tools=tools,
                temperature=temperature,
            )
        except Exception as fallback_exc:
            msg = self._redactor.redact(
                f"Fallaron tanto el proveedor LLM primario ({primary_err}) "
                f"como el fallback secundario ({fallback_exc})."
            )
            logger.error("%s", msg)
            raise LLMProviderError(msg) from None

    def _complete_with_stub(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        assert self._stub_client is not None
        try:
            raw = self._stub_client.chat(
                messages,
                tools=tools,
                model_override=self._config.studio_llm_model,
            )
            return LLMResponse(
                content=str(raw.get("content") or ""),
                tool_calls=_parse_tool_calls(raw.get("tool_calls")),
                model=str(raw.get("model") or self._config.studio_llm_model),
                provider="stub",
            )
        except Exception as exc:
            safe_reason = self._redactor.redact(str(exc))
            logger.warning(
                "Stub primario falló (%s); usando modelo fallback en stub (%s).",
                safe_reason,
                self._config.studio_llm_fallback_model,
            )
            raw_fb = self._stub_client.chat(
                messages,
                tools=tools,
                model_override=self._config.studio_llm_fallback_model,
            )
            return LLMResponse(
                content=str(raw_fb.get("content") or ""),
                tool_calls=_parse_tool_calls(raw_fb.get("tool_calls")),
                model=self._config.studio_llm_fallback_model,
                provider="fallback",
            )

    async def _http_complete(
        self,
        *,
        base_url: str,
        model: str,
        provider: Literal["primary", "fallback"],
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        temperature: float,
    ) -> LLMResponse:
        endpoint = build_chat_completions_url(base_url)
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": False,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                response = await client.post(
                    endpoint,
                    headers=self._headers(),
                    json=payload,
                )
            except httpx.HTTPError as exc:
                safe_err = self._redactor.redact(str(exc))
                raise LLMProviderError(
                    f"Error de red/timeout hacia {endpoint}: {safe_err}"
                ) from None

        if response.status_code >= 400:
            body_preview = self._redactor.redact(response.text[:400])
            raise LLMProviderError(
                f"HTTP {response.status_code} desde {endpoint}: {body_preview}"
            )

        data = response.json()
        choices = data.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict)
            )
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        finish_reason = str(first_choice.get("finish_reason") or "stop")
        resolved_model = str(data.get("model") or model)

        return LLMResponse(
            content=self._redactor.redact(str(content)),
            tool_calls=tool_calls,
            model=resolved_model,
            provider=provider,
            finish_reason=finish_reason,
        )

    async def stream_complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float = 0.2,
        on_token: Callable[[str, str], Any] | None = None,
    ) -> LLMResponse:
        """Completa un turno emitiendo tokens en streaming vía `on_token(delta, model)`."""
        response = await self.complete(
            messages=messages,
            tools=tools,
            temperature=temperature,
        )
        if response.content and on_token is not None:
            async for piece in self._iter_text_chunks(response.content):
                res = on_token(piece, response.model)
                if hasattr(res, "__await__"):
                    await res
        return response

    async def _iter_text_chunks(self, text: str) -> AsyncIterator[str]:
        words = text.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == len(words) - 1 else f"{word} "
            yield piece
