"""Tool-belt v1 del agente de comfy_studio (enqueue_render, search_assets, propose_edl)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ..comfyui_client import ComfyUIClient
from ..config import StudioConfig
from ..progress import ProgressBroker
from ..projects import ProjectStore
from ..templates import (
    TemplateNotFoundError,
    TemplateParamError,
    get_default_registry,
    render_template_workflow,
)

TemplateIdLiteral = Literal[
    "vace_inpaint",
    "vace_restyle",
    "wan_flf2v",
    "wan_i2v",
    "wan_t2v",
    "sdxl_image",
]

VALID_TEMPLATES: tuple[str, ...] = (
    "vace_inpaint",
    "vace_restyle",
    "wan_flf2v",
    "wan_i2v",
    "wan_t2v",
    "sdxl_image",
)


class ToolExecutionError(ValueError):
    """Error de validación o ejecución en una herramienta del agente."""


@dataclass(slots=True)
class ToolContext:
    """Contexto inyectado a cada herramienta durante el loop del agente."""

    project_id: str
    store: ProjectStore
    comfyui_client: ComfyUIClient
    broker: ProgressBroker
    config: StudioConfig


# ---------------------------------------------------------------------------
# Modelos de parámetros de plantillas y herramientas (Pydantic v2)
# ---------------------------------------------------------------------------


class RenderTemplateParams(BaseModel):
    """Parámetros validados para las plantillas de render en ComfyUI.

    Solo `prompt` es obligatorio a nivel de esquema del tool-belt; el resto de
    parámetros son opcionales (`default=None`) para preservar únicamente los
    valores suministrados explícitamente por el LLM o usuario y dejar que cada
    plantilla del registro (`sdxl_image`, `wan_*`, `vace_*`) resuelva sus propios
    defaults y mínimos sin contaminación cruzada (M2-SCR-011).
    """

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(
        ...,
        min_length=1,
        description="Descripción visual principal del plano o imagen a generar.",
    )
    negative_prompt: str | None = Field(
        default=None,
        description=(
            "Elementos a evitar en la generación (si se omite, usa el default de la plantilla)."
        ),
    )
    width: int | None = Field(
        default=None,
        ge=256,
        le=2048,
        description="Ancho en píxeles (832 por defecto para vídeo 480p 16:9; 1024 para SDXL).",
    )
    height: int | None = Field(
        default=None,
        ge=256,
        le=2048,
        description=(
            "Alto en píxeles (480 por defecto para VACE/Wan en 480p + offload; 1024 para SDXL)."
        ),
    )
    frames: int | None = Field(
        default=None,
        ge=1,
        le=241,
        description="Número de fotogramas para vídeo (81 frames ≈ 5 s a 16 fps).",
    )
    steps: int | None = Field(
        default=None,
        ge=1,
        le=100,
        description="Pasos de muestreo (4 con LoRA Lightning para Wan; 25 para SDXL).",
    )
    cfg: float | None = Field(
        default=None,
        ge=1.0,
        le=20.0,
        description="Escala CFG del sampler (1.0 para Wan/VACE; 6.0 para SDXL).",
    )
    seed: int | None = Field(
        default=None,
        ge=0,
        description="Semilla determinista de muestreo (42 por defecto).",
    )
    strength: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Intensidad de restyle o inpainting VACE (0.0 a 1.0).",
    )
    offload: bool | None = Field(
        default=None,
        description=(
            "Activar offload de pesos en plantillas de vídeo Wan 2.2 / VACE (true por defecto)."
        ),
    )
    video_asset_id: str | None = Field(
        default=None,
        description="ID del asset de vídeo base en el proyecto (para vace_inpaint / vace_restyle).",
    )
    mask_asset_id: str | None = Field(
        default=None,
        description="ID del asset de máscara en el proyecto (para vace_inpaint).",
    )
    image_asset_id: str | None = Field(
        default=None,
        description="ID del asset de imagen inicial o referencia (para wan_i2v / vace_restyle).",
    )
    first_frame_asset_id: str | None = Field(
        default=None,
        description="ID del primer fotograma (para wan_flf2v).",
    )
    last_frame_asset_id: str | None = Field(
        default=None,
        description="ID del último fotograma (para wan_flf2v).",
    )

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("El parámetro `prompt` no puede estar vacío.")
        return cleaned


class EnqueueRenderArgs(BaseModel):
    """Argumentos para la herramienta `enqueue_render`."""

    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(
        ...,
        description=(
            "Identificador de la plantilla API-format a ejecutar: "
            "vace_inpaint, vace_restyle, wan_flf2v, wan_i2v, wan_t2v o sdxl_image."
        ),
    )
    params: RenderTemplateParams = Field(
        ...,
        description="Parámetros tipados para la plantilla seleccionada.",
    )

    @field_validator("template_id")
    @classmethod
    def _validate_template_id(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if cleaned not in VALID_TEMPLATES:
            allowed = ", ".join(VALID_TEMPLATES)
            raise ValueError(
                f"Plantilla desconocida {cleaned!r}. Plantillas válidas: {allowed}."
            )
        return cleaned


class SearchAssetsArgs(BaseModel):
    """Argumentos para la herramienta `search_assets`."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="",
        description="Texto libre a buscar en nombre de archivo, categoría, tipo o metadatos.",
    )
    kind: Literal["video", "image", "audio"] | None = Field(
        default=None,
        description="Filtro opcional por tipo de medio (video, image, audio).",
    )
    category: Literal["uploads", "outputs", "keyframes", "voice"] | None = Field(
        default=None,
        description=(
            "Filtro opcional por carpeta del proyecto (uploads, outputs, keyframes, voice)."
        ),
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Número máximo de assets a devolver.",
    )


class EdlCutItem(BaseModel):
    """Un corte individual dentro de una propuesta de EDL."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None,
        description="ID del asset de vídeo fuente en el proyecto.",
    )
    source: str | None = Field(
        default=None,
        description="Nombre del archivo o clip de origen.",
    )
    in_sec: float = Field(
        ...,
        ge=0.0,
        description=" Segundo de inicio del corte (in >= 0).",
    )
    out_sec: float = Field(
        ...,
        gt=0.0,
        description="Segundo de fin del corte (out > in).",
    )
    reason: str = Field(
        default="highlight",
        min_length=1,
        description=(
            "Motivo editorial del corte (highlight, eliminar_pausa, eliminar_muletilla, etc.)."
        ),
    )
    speaker: str | None = Field(
        default=None,
        description="Identificador opcional del hablante (p. ej. SPEAKER_00).",
    )
    text: str | None = Field(
        default=None,
        description="Fragmento opcional de transcripción asociado al corte.",
    )

    @model_validator(mode="after")
    def _check_interval(self) -> EdlCutItem:
        if self.out_sec <= self.in_sec:
            raise ValueError(
                f"Intervalo de corte inválido: out_sec ({self.out_sec}) "
                f"debe ser mayor que in_sec ({self.in_sec})."
            )
        return self


class ProposeEdlArgs(BaseModel):
    """Argumentos para la herramienta `propose_edl`."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        default="Propuesta EDL",
        min_length=1,
        description="Título descriptivo para esta versión del montaje EDL.",
    )
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:5"] = Field(
        default="16:9",
        description=(
            "Relación de aspecto objetivo del montaje (16:9 horizontal, 9:16 reel vertical)."
        ),
    )
    cuts: list[EdlCutItem] = Field(
        ...,
        min_length=1,
        description="Lista ordenada de cortes con tiempos de entrada/salida y razón.",
    )
    notes: str = Field(
        default="",
        description="Notas editoriales sobre pausas o muletillas eliminadas.",
    )


# ---------------------------------------------------------------------------
# Construcción de workflows API-format para enqueue_render
# ---------------------------------------------------------------------------


def _extract_explicit_render_params(
    params: RenderTemplateParams | dict[str, Any],
) -> dict[str, Any]:
    """Extrae únicamente los parámetros suministrados explícitamente por el llamador."""
    if isinstance(params, BaseModel):
        return params.model_dump(exclude_unset=True, exclude_none=True)
    return {k: v for k, v in dict(params).items() if v is not None}


def build_template_workflow(
    template_id: str,
    params: RenderTemplateParams | dict[str, Any],
    *,
    project_id: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    """Construye el grafo API-format de ComfyUI delegando en el registro de plantillas."""
    explicit_params = _extract_explicit_render_params(params)
    return render_template_workflow(
        template_id,
        explicit_params,
        project_id=project_id,
        job_id=job_id,
        strict=True,
    )


# ---------------------------------------------------------------------------
# Handlers de herramientas v1
# ---------------------------------------------------------------------------


async def _handle_enqueue_render(
    args: EnqueueRenderArgs,
    context: ToolContext,
) -> dict[str, Any]:
    explicit_params = _extract_explicit_render_params(args.params)
    registry = get_default_registry()
    try:
        submission = await registry.submit_render(
            args.template_id,
            explicit_params,
            project_id=context.project_id,
            store=context.store,
            comfyui_client=context.comfyui_client,
            broker=context.broker,
            strict=True,
        )
    except (TemplateNotFoundError, TemplateParamError, LookupError) as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "status": submission["status"],
        "job_id": submission["job_id"],
        "prompt_id": submission["prompt_id"],
        "project_id": submission["project_id"],
        "template_id": submission["template_id"],
        "params": submission["params"],
        "outputs": submission["outputs"],
        "gallery_assets": submission["gallery_assets"],
    }


async def _handle_search_assets(
    args: SearchAssetsArgs,
    context: ToolContext,
) -> dict[str, Any]:
    candidates = context.store.list_assets(
        context.project_id,
        kind=args.kind,
        category=args.category,
    )
    tokens = [
        tok.lower()
        for tok in args.query.replace("_", " ").replace("-", " ").split()
        if tok.strip()
    ]
    if tokens:
        filtered: list[dict[str, Any]] = []
        for asset in candidates:
            haystack = " ".join(
                [
                    str(asset.get("filename") or ""),
                    str(asset.get("original_filename") or ""),
                    str(asset.get("kind") or ""),
                    str(asset.get("category") or ""),
                    str(asset.get("mime_type") or ""),
                    json.dumps(asset.get("metadata") or {}, ensure_ascii=False),
                ]
            ).lower().replace("_", " ").replace("-", " ")
            if all(tok in haystack for tok in tokens):
                filtered.append(asset)
        candidates = filtered

    results = candidates[: args.limit]
    return {
        "project_id": context.project_id,
        "query": args.query,
        "kind": args.kind,
        "category": args.category,
        "count": len(results),
        "assets": results,
    }


async def _handle_propose_edl(
    args: ProposeEdlArgs,
    context: ToolContext,
) -> dict[str, Any]:
    cuts_dicts = [cut.model_dump(exclude_none=True) for cut in args.cuts]
    total_duration = round(
        sum(cut.out_sec - cut.in_sec for cut in args.cuts),
        3,
    )
    edl_doc: dict[str, Any] = {
        "schema": "comfy_studio.edl.v1",
        "title": args.title,
        "aspect_ratio": args.aspect_ratio,
        "total_duration_sec": total_duration,
        "cuts": cuts_dicts,
        "notes": args.notes,
    }
    saved = context.store.save_edl_version(
        context.project_id,
        edl=edl_doc,
    )
    context.broker.publish(
        "edl",
        {
            "project_id": context.project_id,
            "edl_id": saved["id"],
            "version": saved["version"],
            "aspect_ratio": args.aspect_ratio,
            "total_duration_sec": total_duration,
            "cuts_count": len(cuts_dicts),
        },
    )
    return {
        "status": "proposed",
        "edl_id": saved["id"],
        "project_id": context.project_id,
        "version": saved["version"],
        "aspect_ratio": args.aspect_ratio,
        "total_duration_sec": total_duration,
        "cuts_count": len(cuts_dicts),
        "edl": saved["edl"],
    }


# ---------------------------------------------------------------------------
# Registro del ToolBelt
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ToolSpec:
    """Definición registrada de una herramienta del agente."""

    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]

    def to_openai_schema(self) -> dict[str, Any]:
        raw_schema = self.args_model.model_json_schema()
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": raw_schema.get("properties", {}),
                    "required": raw_schema.get("required", []),
                    **(
                        {"$defs": raw_schema["$defs"]}
                        if "$defs" in raw_schema
                        else {}
                    ),
                },
            },
        }


class ToolBelt:
    """Registro extensible de herramientas disponibles para el agente LLM."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [spec.to_openai_schema() for spec in self._tools.values()]

    async def execute(
        self,
        name: str,
        raw_arguments: dict[str, Any] | str,
        context: ToolContext,
    ) -> dict[str, Any]:
        spec = self._tools.get(name)
        if spec is None:
            raise ToolExecutionError(f"Herramienta no registrada: {name!r}")

        if isinstance(raw_arguments, str):
            try:
                parsed_args = json.loads(raw_arguments) if raw_arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolExecutionError(
                    f"JSON inválido en argumentos para {name}: {exc}"
                ) from exc
        elif isinstance(raw_arguments, dict):
            parsed_args = dict(raw_arguments)
        else:
            raise ToolExecutionError(
                f"Los argumentos para {name} deben ser un objeto JSON."
            )

        try:
            validated = spec.args_model.model_validate(parsed_args)
        except ValidationError as exc:
            raise ToolExecutionError(
                f"Parámetros inválidos para {name}: {exc}"
            ) from exc

        return await spec.handler(validated, context)


def create_default_tool_belt() -> ToolBelt:
    """Construye el tool-belt v1 con `enqueue_render`, `search_assets` y `propose_edl`."""
    belt = ToolBelt()
    belt.register(
        ToolSpec(
            name="enqueue_render",
            description=(
                "Encola y ejecuta un render de vídeo o imagen en ComfyUI a partir de una "
                "plantilla curada (vace_inpaint, vace_restyle, wan_flf2v, wan_i2v, wan_t2v, "
                "sdxl_image). Valida los parámetros antes de enviar a ComfyUI."
            ),
            args_model=EnqueueRenderArgs,
            handler=_handle_enqueue_render,
        )
    )
    belt.register(
        ToolSpec(
            name="search_assets",
            description=(
                "Busca assets del proyecto actual (vídeos, imágenes, keyframes o audios) "
                "filtrando por texto libre, tipo de medio (kind) o categoría (uploads, outputs, "
                "keyframes, voice)."
            ),
            args_model=SearchAssetsArgs,
            handler=_handle_search_assets,
        )
    )
    belt.register(
        ToolSpec(
            name="propose_edl",
            description=(
                "Propone y guarda una versión de EDL (Edit Decision List) con cortes in/out "
                "en segundos, motivo de cada corte (highlights, pausas, muletillas) y relación "
                "de aspecto objetivo (16:9 o 9:16)."
            ),
            args_model=ProposeEdlArgs,
            handler=_handle_propose_edl,
        )
    )
    return belt
