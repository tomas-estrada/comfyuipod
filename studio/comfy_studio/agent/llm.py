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
                "arguments": self.raw_arguments or json.dumps(self.arguments, ensure_ascii=False),
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
        base_redactor = redactor or config.redactor()
        self._redactor = SecretRedactor(
            studio_llm_key=config.get_llm_key(),
            studio_llm_fallback_key=config.get_llm_fallback_key(),
            comfy_pass=config.get_comfy_pass(),
            extra_secrets=base_redactor.literal_secrets,
        )
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

    def _ensure_fallback_key_available(self) -> str:
        """Garantiza fail-closed para el proveedor de fallback.

        Jamás reutiliza `STUDIO_LLM_KEY` para autenticar contra el proveedor secundario.
        Si `STUDIO_LLM_FALLBACK_KEY` no está configurada o coincide con la primaria,
        aborta antes de realizar ninguna petición al fallback.
        """
        fb_key = self._config.get_llm_fallback_key()
        prim_key = self._config.get_llm_key()
        if not fb_key:
            raise LLMProviderError(
                "STUDIO_LLM_FALLBACK_KEY no está definida "
                "(fail-closed: nunca se reenvía STUDIO_LLM_KEY al proveedor de fallback)."
            )
        if prim_key and fb_key == prim_key:
            raise LLMProviderError(
                "STUDIO_LLM_FALLBACK_KEY debe ser distinta de STUDIO_LLM_KEY (fail-closed)."
            )
        return fb_key

    def _headers(
        self,
        provider: Literal["primary", "fallback"] = "primary",
    ) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if provider == "fallback":
            api_key = self._ensure_fallback_key_available()
        else:
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
        primary_key = self._config.get_llm_key()
        try:
            raw = self._stub_client.chat(
                messages,
                tools=tools,
                model_override=self._config.studio_llm_model,
                provider="primary",
                api_key=primary_key,
                primary_key=primary_key,
            )
            return LLMResponse(
                content=self._redactor.redact(str(raw.get("content") or "")),
                tool_calls=_parse_tool_calls(raw.get("tool_calls")),
                model=str(raw.get("model") or self._config.studio_llm_model),
                provider="stub",
            )
        except Exception as exc:
            primary_err = exc
            safe_reason = self._redactor.redact(str(exc))
            logger.warning(
                "Stub primario falló (%s); usando modelo fallback en stub (%s).",
                safe_reason,
                self._config.studio_llm_fallback_model,
            )
            try:
                fb_key = self._ensure_fallback_key_available()
                raw_fb = self._stub_client.chat(
                    messages,
                    tools=tools,
                    model_override=self._config.studio_llm_fallback_model,
                    provider="fallback",
                    api_key=fb_key,
                    primary_key=primary_key,
                )
                return LLMResponse(
                    content=self._redactor.redact(str(raw_fb.get("content") or "")),
                    tool_calls=_parse_tool_calls(raw_fb.get("tool_calls")),
                    model=self._config.studio_llm_fallback_model,
                    provider="fallback",
                )
            except Exception as fallback_exc:
                msg = self._redactor.redact(
                    f"Fallaron tanto el stub LLM primario ({primary_err}) "
                    f"como el fallback secundario ({fallback_exc})."
                )
                logger.error("%s", msg)
                raise LLMProviderError(msg) from None

    async def _stream_complete_with_stub(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        on_token: Callable[[str, str], Any] | None = None,
    ) -> LLMResponse:
        assert self._stub_client is not None
        primary_key = self._config.get_llm_key()

        async def _run_stub_stream(
            *,
            model_override: str,
            provider_kind: Literal["primary", "fallback"],
            api_key: str | None,
        ) -> tuple[dict[str, Any], str]:
            stream_redactor = self._redactor.create_stream()
            emitted_parts: list[str] = []
            last_model = model_override

            async def _redacted_on_token(piece: str, model_name: str) -> None:
                nonlocal last_model
                if model_name:
                    last_model = model_name
                safe_piece = stream_redactor.push(str(piece))
                if not safe_piece:
                    return
                emitted_parts.append(safe_piece)
                if on_token is not None:
                    res = on_token(safe_piece, last_model)
                    if hasattr(res, "__await__"):
                        await res

            raw_res = await self._stub_client.stream_chat(
                messages,
                tools=tools,
                model_override=model_override,
                provider=provider_kind,
                api_key=api_key,
                primary_key=primary_key,
                on_token=_redacted_on_token,
            )
            resolved_model = str(raw_res.get("model") or last_model)
            tail_piece = stream_redactor.flush()
            if tail_piece:
                emitted_parts.append(tail_piece)
                if on_token is not None:
                    res = on_token(tail_piece, resolved_model)
                    if hasattr(res, "__await__"):
                        await res
            joined_stream = "".join(emitted_parts)
            raw_content = str(raw_res.get("content") or joined_stream)
            return raw_res, self._redactor.redact(raw_content)

        try:
            raw, safe_content = await _run_stub_stream(
                model_override=self._config.studio_llm_model,
                provider_kind="primary",
                api_key=primary_key,
            )
            return LLMResponse(
                content=safe_content,
                tool_calls=_parse_tool_calls(raw.get("tool_calls")),
                model=str(raw.get("model") or self._config.studio_llm_model),
                provider="stub",
            )
        except Exception as exc:
            primary_err = exc
            safe_reason = self._redactor.redact(str(exc))
            logger.warning(
                "Stub primario falló (%s); usando modelo fallback en stub (%s).",
                safe_reason,
                self._config.studio_llm_fallback_model,
            )
            try:
                fb_key = self._ensure_fallback_key_available()
                raw_fb, safe_fb_content = await _run_stub_stream(
                    model_override=self._config.studio_llm_fallback_model,
                    provider_kind="fallback",
                    api_key=fb_key,
                )
                return LLMResponse(
                    content=safe_fb_content,
                    tool_calls=_parse_tool_calls(raw_fb.get("tool_calls")),
                    model=self._config.studio_llm_fallback_model,
                    provider="fallback",
                )
            except Exception as fallback_exc:
                msg = self._redactor.redact(
                    f"Fallaron tanto el stub LLM primario ({primary_err}) "
                    f"como el fallback secundario ({fallback_exc})."
                )
                logger.error("%s", msg)
                raise LLMProviderError(msg) from None

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
        headers = self._headers(provider)
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
                    headers=headers,
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

    async def _http_stream_complete(
        self,
        *,
        base_url: str,
        model: str,
        provider: Literal["primary", "fallback"],
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]] | None,
        temperature: float,
        on_token: Callable[[str, str], Any] | None = None,
    ) -> LLMResponse:
        headers = self._headers(provider)
        endpoint = build_chat_completions_url(base_url)
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = list(tools)
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
        ) as client:
            try:
                async with client.stream(
                    "POST",
                    endpoint,
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        raw_err = await response.aread()
                        err_text = raw_err.decode("utf-8", errors="replace")
                        body_preview = self._redactor.redact(err_text[:400])
                        raise LLMProviderError(
                            f"HTTP {response.status_code} desde {endpoint}: {body_preview}"
                        )

                    content_type = (response.headers.get("content-type") or "").lower()
                    if "text/event-stream" in content_type:
                        content_parts: list[str] = []
                        tc_accum: dict[int, dict[str, Any]] = {}
                        resolved_model = model
                        finish_reason = "stop"
                        stream_redactor = self._redactor.create_stream()

                        async for raw_line in response.aiter_lines():
                            line = raw_line.strip()
                            if not line or line.startswith(":"):
                                continue
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if not data_str:
                                continue
                            if data_str == "[DONE]":
                                break
                            try:
                                chunk = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue
                            if not isinstance(chunk, dict):
                                continue

                            if chunk.get("model"):
                                resolved_model = str(chunk["model"])
                            choices = chunk.get("choices") or []
                            first_choice = choices[0] if choices else {}
                            if first_choice.get("finish_reason"):
                                finish_reason = str(first_choice["finish_reason"])

                            delta = (
                                first_choice.get("delta")
                                or first_choice.get("message")
                                or {}
                            )
                            if not isinstance(delta, dict):
                                continue

                            piece = delta.get("content")
                            if isinstance(piece, list):
                                piece = "".join(
                                    str(part.get("text", ""))
                                    for part in piece
                                    if isinstance(part, dict)
                                )
                            if piece:
                                safe_piece = stream_redactor.push(str(piece))
                                if safe_piece:
                                    content_parts.append(safe_piece)
                                    if on_token is not None:
                                        cb_res = on_token(safe_piece, resolved_model)
                                        if hasattr(cb_res, "__await__"):
                                            await cb_res

                            delta_tcs = delta.get("tool_calls")
                            if isinstance(delta_tcs, list):
                                for pos, raw_tc in enumerate(delta_tcs):
                                    if not isinstance(raw_tc, dict):
                                        continue
                                    tc_idx = int(raw_tc.get("index", pos))
                                    entry = tc_accum.setdefault(
                                        tc_idx,
                                        {
                                            "id": "",
                                            "type": "function",
                                            "function": {"name": "", "arguments": ""},
                                        },
                                    )
                                    if raw_tc.get("id"):
                                        entry["id"] = str(raw_tc["id"])
                                    fn = raw_tc.get("function") or {}
                                    if isinstance(fn, dict):
                                        if fn.get("name"):
                                            entry["function"]["name"] += str(fn["name"])
                                        if "arguments" in fn and fn["arguments"] is not None:
                                            if isinstance(fn["arguments"], dict):
                                                entry["function"]["arguments"] = json.dumps(
                                                    fn["arguments"], ensure_ascii=False
                                                )
                                            else:
                                                entry["function"]["arguments"] += str(
                                                    fn["arguments"]
                                                )

                        tail_piece = stream_redactor.flush()
                        if tail_piece:
                            content_parts.append(tail_piece)
                            if on_token is not None:
                                cb_res = on_token(tail_piece, resolved_model)
                                if hasattr(cb_res, "__await__"):
                                    await cb_res

                        raw_tool_calls = [tc_accum[k] for k in sorted(tc_accum)]
                        return LLMResponse(
                            content=self._redactor.redact("".join(content_parts)),
                            tool_calls=_parse_tool_calls(raw_tool_calls),
                            model=resolved_model,
                            provider=provider,
                            finish_reason=finish_reason,
                        )

                    # Fallback compatible para respuestas JSON no-SSE a una petición stream=True
                    raw_body = await response.aread()
                    data = json.loads(raw_body.decode("utf-8"))
            except httpx.HTTPError as exc:
                safe_err = self._redactor.redact(str(exc))
                raise LLMProviderError(
                    f"Error de red/timeout hacia {endpoint}: {safe_err}"
                ) from None

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
        safe_content = self._redactor.redact(str(content))
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
        finish_reason = str(first_choice.get("finish_reason") or "stop")
        resolved_model = str(data.get("model") or model)

        if safe_content and on_token is not None:
            async for piece in self._iter_text_chunks(safe_content):
                res = on_token(piece, resolved_model)
                if hasattr(res, "__await__"):
                    await res

        return LLMResponse(
            content=safe_content,
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
        """Completa un turno con streaming incremental real (`stream=True` / SSE)."""
        validate_no_auth_urls_in_messages(messages)

        if self._stub_client is not None and self._transport is None:
            return await self._stream_complete_with_stub(
                messages,
                tools=tools,
                on_token=on_token,
            )

        tokens_emitted = False
        tracked_on_token = on_token
        if on_token is not None:

            def _tracking_on_token(piece: str, model_name: str) -> Any:
                nonlocal tokens_emitted
                tokens_emitted = True
                return on_token(piece, model_name)

            tracked_on_token = _tracking_on_token

        primary_err: Exception | None = None
        try:
            return await self._http_stream_complete(
                base_url=self._config.studio_llm_base_url,
                model=self._config.studio_llm_model,
                provider="primary",
                messages=messages,
                tools=tools,
                temperature=temperature,
                on_token=tracked_on_token,
            )
        except Exception as exc:
            safe_reason = self._redactor.redact(str(exc))
            if tokens_emitted:
                logger.error(
                    "Proveedor LLM primario (%s / %s) falló a mitad de stream tras "
                    "emitir tokens; no se activa el fallback para no duplicar el "
                    "texto ya entregado (%s).",
                    self._config.studio_llm_base_url,
                    self._config.studio_llm_model,
                    safe_reason,
                )
                if isinstance(exc, LLMProviderError):
                    raise
                raise LLMProviderError(
                    f"El proveedor LLM primario falló a mitad de stream: {safe_reason}"
                ) from None
            primary_err = exc
            logger.warning(
                "Proveedor LLM primario (%s / %s) falló en streaming (%s). "
                "Activando fallback secundario (%s / %s).",
                self._config.studio_llm_base_url,
                self._config.studio_llm_model,
                safe_reason,
                self._config.studio_llm_fallback_base_url,
                self._config.studio_llm_fallback_model,
            )

        try:
            return await self._http_stream_complete(
                base_url=self._config.studio_llm_fallback_base_url,
                model=self._config.studio_llm_fallback_model,
                provider="fallback",
                messages=messages,
                tools=tools,
                temperature=temperature,
                on_token=on_token,
            )
        except Exception as fallback_exc:
            msg = self._redactor.redact(
                f"Fallaron tanto el proveedor LLM primario ({primary_err}) "
                f"como el fallback secundario ({fallback_exc})."
            )
            logger.error("%s", msg)
            raise LLMProviderError(msg) from None

    async def _iter_text_chunks(self, text: str) -> AsyncIterator[str]:
        words = text.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == len(words) - 1 else f"{word} "
            yield piece
