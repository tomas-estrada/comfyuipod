"""Doble determinista compatible con OpenAI para STUDIO_STUB_LLM=1."""

from __future__ import annotations

import json
import uuid
from collections import deque
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StubToolCallSpec:
    """Especificación de una llamada a herramienta simulada por el stub LLM."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None

    def to_openai_dict(self) -> dict[str, Any]:
        cid = self.call_id or f"call_{uuid.uuid4().hex[:10]}"
        return {
            "id": cid,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class StubLLMstep:
    """Paso programado para una invocación del stub compatible-OpenAI."""

    content: str = ""
    tool_calls: list[StubToolCallSpec] = field(default_factory=list)
    fail_with_status: int | None = None
    fail_with_timeout: bool = False
    error_message: str = "Simulated upstream provider error"


def _extract_last_user_text(messages: Sequence[dict[str, Any]]) -> str:
    for item in reversed(messages):
        if item.get("role") == "user":
            content = item.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                return " ".join(t for t in texts if t).strip()
            return str(content)
    return ""


class StubLLMClient:
    """Cliente LLM determinista en memoria que jamás realiza llamadas de red."""

    def __init__(self, model: str = "qwen3-vl-plus") -> None:
        self.model = model
        self.calls: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []
        self._steps: deque[StubLLMstep] = deque()
        self.fail_primary_status: int | None = None
        self.fail_primary_timeout: bool = False

    def enqueue_step(self, step: StubLLMstep) -> None:
        """Encola un paso determinista (texto y/o tool_calls) para la siguiente llamada."""
        self._steps.append(step)

    def clear_steps(self) -> None:
        self._steps.clear()

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        msg_list = [dict(m) for m in messages]
        self.calls.append(msg_list)
        self.tools_seen.append([dict(t) for t in (tools or [])])
        active_model = model_override or self.model

        if self._steps:
            step = self._steps.popleft()
            if step.fail_with_timeout:
                raise TimeoutError(step.error_message)
            if step.fail_with_status is not None:
                raise RuntimeError(f"HTTP {step.fail_with_status}: {step.error_message}")
            return {
                "role": "assistant",
                "model": active_model,
                "stub": True,
                "content": step.content,
                "tool_calls": [tc.to_openai_dict() for tc in step.tool_calls],
                "tools_available": len(tools) if tools else 0,
            }

        last_user = _extract_last_user_text(msg_list)
        return {
            "role": "assistant",
            "model": active_model,
            "stub": True,
            "content": f"[stub-llm:{active_model}] Respuesta simulada para: {last_user[:120]}",
            "tool_calls": [],
            "tools_available": len(tools) if tools else 0,
        }

    async def stream_tokens(self, text: str) -> AsyncIterator[str]:
        """Divide un texto en fragmentos deterministas para simular SSE de tokens."""
        if not text:
            return
        words = text.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == len(words) - 1 else f"{word} "
            yield piece
