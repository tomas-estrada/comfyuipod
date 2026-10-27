"""Paquete del agente LLM compatible-OpenAI de comfy_studio."""

from __future__ import annotations

from .llm import (
    LLMProviderError,
    LLMResponse,
    LLMToolCall,
    OpenAICompatibleLLMClient,
    build_vision_content_part,
    encode_image_to_data_url,
    validate_no_auth_urls_in_messages,
)
from .loop import AgentTurnResult, StudioAgent
from .tools import (
    EnqueueRenderArgs,
    ProposeEdlArgs,
    RenderTemplateParams,
    SearchAssetsArgs,
    ToolBelt,
    ToolContext,
    ToolExecutionError,
    ToolSpec,
    create_default_tool_belt,
)

__all__ = [
    "AgentTurnResult",
    "EnqueueRenderArgs",
    "LLMProviderError",
    "LLMResponse",
    "LLMToolCall",
    "OpenAICompatibleLLMClient",
    "ProposeEdlArgs",
    "RenderTemplateParams",
    "SearchAssetsArgs",
    "StudioAgent",
    "ToolBelt",
    "ToolContext",
    "ToolExecutionError",
    "ToolSpec",
    "build_vision_content_part",
    "create_default_tool_belt",
    "encode_image_to_data_url",
    "validate_no_auth_urls_in_messages",
]
