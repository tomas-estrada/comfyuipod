"""Doble determinista compatible con OpenAI para STUDIO_STUB_LLM=1."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


class StubLLMClient:
    """Cliente LLM determinista en memoria que jamás realiza llamadas de red."""

    def __init__(self, model: str = "qwen3-vl-plus") -> None:
        self.model = model
        self.calls: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        msg_list = [dict(m) for m in messages]
        self.calls.append(msg_list)
        last_user = ""
        for item in reversed(msg_list):
            if item.get("role") == "user":
                content = item.get("content", "")
                last_user = content if isinstance(content, str) else str(content)
                break
        return {
            "role": "assistant",
            "model": self.model,
            "stub": True,
            "content": f"[stub-llm:{self.model}] Respuesta simulada para: {last_user[:120]}",
            "tool_calls": [],
            "tools_available": len(tools) if tools else 0,
        }
