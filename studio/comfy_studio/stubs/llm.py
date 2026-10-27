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
        auto_calls, plan_text = self._build_auto_tool_calls(
            msg_list,
            tools=tools,
            last_user=last_user,
            active_model=active_model,
        )
        if auto_calls:
            return {
                "role": "assistant",
                "model": active_model,
                "stub": True,
                "content": plan_text,
                "tool_calls": [tc.to_openai_dict() for tc in auto_calls],
                "tools_available": len(tools) if tools else 0,
            }

        return {
            "role": "assistant",
            "model": active_model,
            "stub": True,
            "content": f"[stub-llm:{active_model}] Respuesta simulada para: {last_user[:120]}",
            "tool_calls": [],
            "tools_available": len(tools) if tools else 0,
        }

    def _build_auto_tool_calls(
        self,
        msg_list: Sequence[dict[str, Any]],
        *,
        tools: Sequence[dict[str, Any]] | None,
        last_user: str,
        active_model: str,
    ) -> tuple[list[StubToolCallSpec], str]:
        if not tools or not msg_list or msg_list[-1].get("role") != "user":
            return [], ""

        available_tools = {
            str(t.get("function", {}).get("name") or "")
            for t in tools
            if isinstance(t, dict)
        }
        lower = last_user.lower()

        render_keywords = (
            "wan_t2v",
            "wan_i2v",
            "wan_flf2v",
            "vace_inpaint",
            "vace_restyle",
            "sdxl_image",
            "render",
            "genera un vídeo",
            "genera un video",
            "genera un clip",
            "generar vídeo",
            "generar video",
        )
        if "enqueue_render" in available_tools and any(k in lower for k in render_keywords):
            if "vace_inpaint" in lower or "inpaint" in lower:
                template_id = "vace_inpaint"
            elif "vace_restyle" in lower or "restyle" in lower:
                template_id = "vace_restyle"
            elif "wan_flf2v" in lower or "flf2v" in lower:
                template_id = "wan_flf2v"
            elif "wan_i2v" in lower or "i2v" in lower:
                template_id = "wan_i2v"
            elif "sdxl_image" in lower or "sdxl" in lower:
                template_id = "sdxl_image"
            else:
                template_id = "wan_t2v"

            calls: list[StubToolCallSpec] = []
            if "search_assets" in available_tools:
                calls.append(
                    StubToolCallSpec(
                        name="search_assets",
                        arguments={"query": ""},
                    )
                )
            calls.append(
                StubToolCallSpec(
                    name="enqueue_render",
                    arguments={
                        "template_id": template_id,
                        "params": {"prompt": last_user[:200] or "Escena de estudio"},
                    },
                )
            )
            plan = (
                f"[stub-llm:{active_model}] Plan: consultando medios del proyecto "
                f"y encolando render con plantilla {template_id}."
            )
            return calls, plan

        edl_keywords = ("edl", "highlights", "muletilla", "línea de tiempo", "timeline")
        if "propose_edl" in available_tools and any(k in lower for k in edl_keywords):
            calls = [
                StubToolCallSpec(
                    name="propose_edl",
                    arguments={
                        "title": "EDL Highlights Automático",
                        "aspect_ratio": "16:9",
                        "notes": f"Propuesta generada para: {last_user[:80]}",
                        "cuts": [
                            {
                                "in_sec": 0.0,
                                "out_sec": 4.5,
                                "reason": "Apertura limpia sin silencios ni muletillas",
                                "speaker": "Locutor",
                            },
                            {
                                "in_sec": 6.0,
                                "out_sec": 12.0,
                                "reason": "Highlight principal y cierre",
                                "speaker": "Locutor",
                            },
                        ],
                    },
                )
            ]
            plan = f"[stub-llm:{active_model}] Plan: proponiendo EDL para «{last_user[:60]}»."
            return calls, plan

        search_keywords = (
            "busca",
            "buscar",
            "listar medios",
            "lista los medios",
            "qué medios",
            "que medios",
            "assets del proyecto",
        )
        if "search_assets" in available_tools and any(k in lower for k in search_keywords):
            calls = [
                StubToolCallSpec(
                    name="search_assets",
                    arguments={"query": ""},
                )
            ]
            plan = f"[stub-llm:{active_model}] Plan: buscando assets del proyecto."
            return calls, plan

        return [], ""

    async def stream_tokens(self, text: str) -> AsyncIterator[str]:
        """Divide un texto en fragmentos deterministas para simular SSE de tokens."""
        if not text:
            return
        words = text.split(" ")
        for idx, word in enumerate(words):
            piece = word if idx == len(words) - 1 else f"{word} "
            yield piece
