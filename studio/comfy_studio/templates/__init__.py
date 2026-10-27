"""Paquete de plantillas de render ComfyUI en formato API para Comfy Studio."""

from __future__ import annotations

from .registry import (
    ASSET_PARAM_TO_INPUT_PARAM,
    EXPECTED_M2_TEMPLATE_IDS,
    TemplateNotFoundError,
    TemplateParamError,
    TemplateRegistry,
    build_json_schema_from_parameters,
    get_default_registry,
    render_template_workflow,
)
from .validator import (
    BUILTIN_CONTEXT_PLACEHOLDERS,
    KNOWN_NODE_SPECS,
    TERMINAL_OUTPUT_CLASSES,
    TemplateValidationError,
    extract_placeholders,
    validate_all_templates,
    validate_template_definition,
    validate_template_file,
    validate_templates_dir,
)

__all__ = [
    "ASSET_PARAM_TO_INPUT_PARAM",
    "BUILTIN_CONTEXT_PLACEHOLDERS",
    "EXPECTED_M2_TEMPLATE_IDS",
    "KNOWN_NODE_SPECS",
    "TERMINAL_OUTPUT_CLASSES",
    "TemplateNotFoundError",
    "TemplateParamError",
    "TemplateRegistry",
    "TemplateValidationError",
    "build_json_schema_from_parameters",
    "extract_placeholders",
    "get_default_registry",
    "render_template_workflow",
    "validate_all_templates",
    "validate_template_definition",
    "validate_template_file",
    "validate_templates_dir",
]
