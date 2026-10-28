"""Tool-belt v1 del agente de comfy_studio (enqueue_render, search_assets, propose_edl)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool

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
    "hunyuan3d_image_to_3d",
]

VALID_TEMPLATES: tuple[str, ...] = (
    "vace_inpaint",
    "vace_restyle",
    "wan_flf2v",
    "wan_i2v",
    "wan_t2v",
    "sdxl_image",
    "hunyuan3d_image_to_3d",
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
    delegate_runner: Callable[..., Awaitable[dict[str, Any]]] | None = None


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

    prompt: str | None = Field(
        default=None,
        description=(
            "Descripción visual principal del plano o imagen a generar "
            "(obligatorio salvo en plantillas 3D)."
        ),
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
        description=(
            "ID del asset de imagen inicial o referencia "
            "(para wan_i2v / vace_restyle / hunyuan3d)."
        ),
    )
    first_frame_asset_id: str | None = Field(
        default=None,
        description="ID del primer fotograma (para wan_flf2v).",
    )
    last_frame_asset_id: str | None = Field(
        default=None,
        description="ID del último fotograma (para wan_flf2v).",
    )
    latent_resolution: int | None = Field(
        default=None,
        ge=512,
        le=8192,
        description="Resolución latente del flujo 3D (4096 por defecto).",
    )
    octree_resolution: int | None = Field(
        default=None,
        ge=64,
        le=1024,
        description="Resolución de octree para vóxeles 3D (256 por defecto).",
    )
    mesh_algorithm: str | None = Field(
        default=None,
        description="Algoritmo de extracción de malla (surface_net por defecto).",
    )
    mesh_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Umbral de extracción de superficie para la malla.",
    )

    @field_validator("prompt")
    @classmethod
    def _prompt_not_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
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
            "vace_inpaint, vace_restyle, wan_flf2v, wan_i2v, wan_t2v, "
            "sdxl_image o hunyuan3d_image_to_3d."
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
            raise ValueError(f"Plantilla desconocida {cleaned!r}. Plantillas válidas: {allowed}.")
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


class ExtractKeyframesArgs(BaseModel):
    """Argumentos para la herramienta `extract_keyframes`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None,
        description=(
            "ID opcional del asset de vídeo en el proyecto "
            "(usa el primero disponible si se omite)."
        ),
    )
    interval_sec: float = Field(
        default=2.0,
        ge=0.25,
        le=60.0,
        description="Intervalo de muestreo en segundos entre keyframes.",
    )
    scene_threshold: float = Field(
        default=0.25,
        ge=0.05,
        le=0.95,
        description="Umbral de detección de corte de escena (0.05 a 0.95).",
    )
    max_frames: int = Field(
        default=24,
        ge=1,
        le=200,
        description="Número máximo de keyframes a extraer.",
    )


class IndexMediaArgs(BaseModel):
    """Argumentos para la herramienta `index_media`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None,
        description=(
            "ID opcional del asset a indexar con CLIP e InsightFace "
            "(indexa todo el proyecto si se omite)."
        ),
    )
    interval_sec: float = Field(
        default=2.0,
        ge=0.25,
        le=60.0,
        description="Intervalo de muestreo de keyframes en segundos para vídeos.",
    )
    detect_faces: bool = Field(
        default=True,
        description="Ejecutar también detección de rostros con InsightFace sobre cada plano.",
    )


class FindShotsTextArgs(BaseModel):
    """Argumentos para la herramienta `find_shots_text`."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        description="Consulta semántica en lenguaje natural (p. ej. 'plano con gorra verde').",
    )
    asset_id: str | None = Field(
        default=None,
        description="Filtro opcional por asset de origen.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Número máximo de planos ordenados por similitud CLIP a devolver.",
    )


class FindShotsFaceArgs(BaseModel):
    """Argumentos para la herramienta `find_shots_face`."""

    model_config = ConfigDict(extra="forbid")

    identity_name: str | None = Field(
        default=None,
        description="Nombre de la identidad registrada a buscar (p. ej. 'Ana').",
    )
    identity_id: str | None = Field(
        default=None,
        description="ID de la identidad registrada en el proyecto.",
    )
    reference_asset_id: str | None = Field(
        default=None,
        description="ID de un asset de imagen con rostro de referencia.",
    )
    text_query: str | None = Field(
        default=None,
        description="Filtro semántico CLIP adicional para búsqueda combinada (texto ∩ persona).",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Número máximo de planos coincidentes a devolver.",
    )


class TranscribeArgs(BaseModel):
    """Argumentos para la herramienta `transcribe`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None,
        description=(
            "ID del asset de audio o vídeo a transcribir "
            "(usa el primero del proyecto si se omite)."
        ),
    )
    language: str = Field(
        default="es",
        min_length=2,
        max_length=10,
        description="Código de idioma para whisperx y el aligner wav2vec2 (es, en).",
    )


class DiarizeArgs(BaseModel):
    """Argumentos para la herramienta `diarize`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None,
        description="ID del asset de audio o vídeo a diarizar con FunASR cam++.",
    )
    transcription_id: str | None = Field(
        default=None,
        description="ID opcional de la transcripción sobre la cual fusionar hablantes.",
    )
    expected_speakers: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description="Número estimado de hablantes en el archivo.",
    )


class BuildEdlArgs(BaseModel):
    """Argumentos para la herramienta `build_edl`."""

    model_config = ConfigDict(extra="forbid")

    edl_id: str | None = Field(
        default=None,
        description="ID de la versión de EDL a renderizar con ffmpeg (usa la última si se omite).",
    )
    version: int | None = Field(
        default=None,
        ge=1,
        description="Número de versión del EDL a ejecutar.",
    )
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:5"] | None = Field(
        default=None,
        description=(
            "Sobrescribe la relación de aspecto de entrega (16:9 horizontal, 9:16 vertical)."
        ),
    )
    source_asset_id: str | None = Field(
        default=None,
        description="ID opcional del vídeo fuente sobre el que aplicar los cortes.",
    )
    from_transcription: bool = Field(
        default=False,
        description=(
            "Si es true y no hay EDL previo, genera primero un EDL automático "
            "de highlights desde la transcripción."
        ),
    )


class ListTemplatesArgs(BaseModel):
    """Argumentos para la herramienta `list_templates`."""

    model_config = ConfigDict(extra="forbid")

    include_workflow: bool = Field(
        default=False,
        description="Incluir el grafo de workflow completo de cada plantilla en la respuesta.",
    )


class JobStatusArgs(BaseModel):
    """Argumentos para la herramienta `job_status`."""

    model_config = ConfigDict(extra="forbid")

    job_id: str | None = Field(
        default=None,
        description=(
            "ID de un job concreto del proyecto activo. Si se omite, devuelve los "
            "jobs más recientes del proyecto."
        ),
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Número máximo de jobs recientes a devolver cuando no se indica job_id.",
    )


class JobCancelArgs(BaseModel):
    """Argumentos para la herramienta `job_cancel`."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(
        ...,
        min_length=1,
        description=(
            "ID del job del proyecto activo a cancelar (best-effort: interrumpe la "
            "ejecución activa de ComfyUI y lo marca como cancelado)."
        ),
    )


class TagAssetArgs(BaseModel):
    """Argumentos para la herramienta `tag_asset`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str = Field(..., min_length=1, description="ID del asset del proyecto activo.")
    tags: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Etiquetas libres a añadir al asset (se fusionan con las existentes).",
    )
    note: str | None = Field(
        default=None,
        max_length=500,
        description="Nota editorial opcional guardada en los metadatos del asset.",
    )


class SystemStatusArgs(BaseModel):
    """Argumentos para la herramienta `system_status`."""

    model_config = ConfigDict(extra="forbid")


class ExportEdlArgs(BaseModel):
    """Argumentos para la herramienta `export_edl`."""

    model_config = ConfigDict(extra="forbid")

    version: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Número de versión del EDL a exportar (usa la última versión si se omite)."
        ),
    )


class PlanStepItem(BaseModel):
    """Un paso individual del plan del agente."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=400, description="Descripción del paso.")
    status: Literal["pending", "in_progress", "done", "skipped"] = Field(
        default="pending",
        description="Estado de progreso del paso.",
    )


class UpdatePlanArgs(BaseModel):
    """Argumentos para la herramienta `update_plan`."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(
        default="",
        max_length=200,
        description="Título corto del plan (p. ej. 'Montar highlights del entrevista').",
    )
    steps: list[PlanStepItem] = Field(
        default_factory=list,
        max_length=30,
        description="Lista ordenada de pasos con su estado.",
    )
    status: Literal["active", "completed", "abandoned"] = Field(
        default="active",
        description="Estado global del plan.",
    )
    plan_id: str | None = Field(
        default=None,
        description=(
            "ID del plan a actualizar (el plan activo del proyecto si se omite; "
            "crea uno nuevo si no existe plan activo)."
        ),
    )


RoleLiteral = Literal["director", "editor", "artist3d"]

ROLE_TOOL_SUBSETS: dict[str, tuple[str, ...]] = {
    "director": (
        "enqueue_render",
        "list_templates",
        "search_assets",
        "job_status",
        "job_cancel",
        "system_status",
        "tag_asset",
    ),
    "editor": (
        "propose_edl",
        "build_edl",
        "export_edl",
        "transcribe",
        "diarize",
        "extract_keyframes",
        "find_shots_text",
        "search_assets",
    ),
    "artist3d": (
        "enqueue_render",
        "list_templates",
        "search_assets",
        "tag_asset",
        "job_status",
        "index_media",
        "find_shots_text",
    ),
}


class DelegateRoleArgs(BaseModel):
    """Argumentos para la herramienta `delegate_role`."""

    model_config = ConfigDict(extra="forbid")

    role: RoleLiteral = Field(
        ...,
        description=(
            "Rol especializado: 'director' (renders/parámetros), 'editor' (EDL/montaje/"
            "transcripción) o 'artist3d' (assets visuales/3D e indexado)."
        ),
    )
    task: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description=(
            "Instrucción concreta y autocontenida para el sub-agente (qué debe lograr y "
            "qué debe devolver)."
        ),
    )
    max_iterations: int = Field(
        default=4,
        ge=1,
        le=4,
        description="Tope de iteraciones de herramienta del sub-agente (máximo 4).",
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
# Handlers de herramientas v1 + M3 (percepción, ASR, diarización y EDL)
# ---------------------------------------------------------------------------


def _get_vector_store(context: ToolContext):
    from ..perception.store import PerceptionVectorStore

    return PerceptionVectorStore(context.config.studio_data_dir / "index.db")


async def _handle_enqueue_render(
    args: EnqueueRenderArgs,
    context: ToolContext,
) -> dict[str, Any]:
    if args.template_id != "hunyuan3d_image_to_3d" and not (
        args.params.prompt and args.params.prompt.strip()
    ):
        raise ToolExecutionError(
            f"La plantilla {args.template_id!r} requiere el parámetro 'prompt'."
        )
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
        tok.lower() for tok in args.query.replace("_", " ").replace("-", " ").split() if tok.strip()
    ]
    if tokens:
        filtered: list[dict[str, Any]] = []
        for asset in candidates:
            haystack = (
                " ".join(
                    [
                        str(asset.get("filename") or ""),
                        str(asset.get("original_filename") or ""),
                        str(asset.get("kind") or ""),
                        str(asset.get("category") or ""),
                        str(asset.get("mime_type") or ""),
                        json.dumps(asset.get("metadata") or {}, ensure_ascii=False),
                    ]
                )
                .lower()
                .replace("_", " ")
                .replace("-", " ")
            )
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


async def _handle_extract_keyframes(
    args: ExtractKeyframesArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..perception.keyframes import extract_video_keyframes

    target_aid = args.asset_id
    if not target_aid:
        videos = context.store.list_assets(context.project_id, kind="video")
        if not videos:
            raise ToolExecutionError(
                "El proyecto no tiene ningún asset de vídeo para extraer keyframes."
            )
        target_aid = videos[0]["id"]

    try:
        keyframes = await run_in_threadpool(
            extract_video_keyframes,
            context.store,
            context.project_id,
            target_aid,
            interval_sec=args.interval_sec,
            scene_threshold=args.scene_threshold,
            max_frames=args.max_frames,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "status": "extracted",
        "project_id": context.project_id,
        "source_asset_id": target_aid,
        "count": len(keyframes),
        "keyframes": keyframes,
    }


async def _handle_index_media(
    args: IndexMediaArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..perception.clip_index import ClipVisualIndex
    from ..perception.face_index import InsightFaceIndex

    vec_store = _get_vector_store(context)
    clip_idx = ClipVisualIndex(context.store, vec_store)
    face_idx = InsightFaceIndex(context.store, vec_store)

    def _do_index() -> dict[str, Any]:
        if args.asset_id:
            clip_res = clip_idx.index_asset(
                context.project_id,
                args.asset_id,
                interval_sec=args.interval_sec,
            )
            face_count = 0
            if args.detect_faces:
                dets = face_idx.detect_and_index_asset(context.project_id, args.asset_id)
                face_count += len(dets)
                for shot in clip_res.get("shots", []):
                    kf_id = shot.get("keyframe_asset_id")
                    if kf_id and kf_id != args.asset_id:
                        kf_dets = face_idx.detect_and_index_asset(
                            context.project_id,
                            kf_id,
                            source_asset_id=args.asset_id,
                            timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                        )
                        face_count += len(kf_dets)
            return {
                "status": "indexed",
                "project_id": context.project_id,
                "asset_id": args.asset_id,
                "shots_indexed": clip_res["shots_indexed"],
                "faces_detected": face_count,
                "shots": clip_res["shots"],
            }

        proj_res = clip_idx.index_project_media(
            context.project_id,
            interval_sec=args.interval_sec,
        )
        face_count = 0
        if args.detect_faces:
            for item in proj_res.get("results", []):
                src_aid = item["asset_id"]
                for shot in item.get("shots", []):
                    kf_id = shot.get("keyframe_asset_id") or src_aid
                    dets = face_idx.detect_and_index_asset(
                        context.project_id,
                        kf_id,
                        source_asset_id=src_aid,
                        timestamp_sec=float(shot.get("timestamp_sec") or 0.0),
                    )
                    face_count += len(dets)
        return {
            "status": "indexed",
            "project_id": context.project_id,
            "assets_indexed": proj_res["assets_indexed"],
            "shots_indexed": proj_res["shots_indexed"],
            "faces_detected": face_count,
        }

    try:
        return await run_in_threadpool(_do_index)
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc


async def _handle_find_shots_text(
    args: FindShotsTextArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..perception.clip_index import ClipVisualIndex

    vec_store = _get_vector_store(context)
    clip_idx = ClipVisualIndex(context.store, vec_store)
    shots = clip_idx.find_shots_text(
        context.project_id,
        args.query,
        limit=args.limit,
        asset_id=args.asset_id,
    )
    return {
        "project_id": context.project_id,
        "query": args.query,
        "count": len(shots),
        "shots": shots,
    }


async def _handle_find_shots_face(
    args: FindShotsFaceArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..perception.face_index import InsightFaceIndex

    vec_store = _get_vector_store(context)
    face_idx = InsightFaceIndex(context.store, vec_store)
    try:
        shots = face_idx.find_shots_face(
            context.project_id,
            identity_id=args.identity_id,
            identity_name=args.identity_name,
            reference_asset_id=args.reference_asset_id,
            text_query=args.text_query,
            limit=args.limit,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "project_id": context.project_id,
        "identity_name": args.identity_name,
        "identity_id": args.identity_id,
        "text_query": args.text_query,
        "count": len(shots),
        "shots": shots,
    }


async def _handle_transcribe(
    args: TranscribeArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..asr.whisperx import transcribe_project_asset

    try:
        result = await run_in_threadpool(
            transcribe_project_asset,
            context.store,
            context.project_id,
            asset_id=args.asset_id,
            language=args.language,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "status": "transcribed",
        "project_id": context.project_id,
        "transcription_id": result["id"],
        "asset_id": result["asset_id"],
        "language": result["language"],
        "segments_count": len(result.get("segments") or []),
        "words_count": len(result.get("words") or []),
        "transcription": result,
    }


async def _handle_diarize(
    args: DiarizeArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..asr.diarize import diarize_project_asset

    try:
        result = await run_in_threadpool(
            diarize_project_asset,
            context.store,
            context.project_id,
            asset_id=args.asset_id,
            transcription_id=args.transcription_id,
            expected_speakers=args.expected_speakers,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc

    return {
        "status": "diarized",
        "project_id": context.project_id,
        "transcription_id": result["transcription"]["id"],
        "speakers": result["speakers"],
        "by_speaker": result["by_speaker"],
        "diarization": result["diarization"],
    }


async def _handle_build_edl(
    args: BuildEdlArgs,
    context: ToolContext,
) -> dict[str, Any]:
    from ..edl.ffmpeg_edl import execute_edl_ffmpeg
    from ..edl.highlights import generate_highlights_edl

    def _do_build_edl() -> dict[str, Any]:
        edl_id = args.edl_id
        if edl_id is None and args.version is None:
            latest = context.store.get_latest_edl(context.project_id)
            if latest is None and args.from_transcription:
                generated = generate_highlights_edl(
                    context.store,
                    context.project_id,
                    asset_id=args.source_asset_id,
                    aspect_ratio=args.aspect_ratio or "16:9",
                )
                edl_id = generated["id"]
        return execute_edl_ffmpeg(
            context.store,
            context.project_id,
            edl_id=edl_id,
            version=args.version,
            aspect_ratio=args.aspect_ratio,
            source_asset_id=args.source_asset_id,
        )

    try:
        result = await run_in_threadpool(_do_build_edl)
    except (ValueError, RuntimeError) as exc:
        raise ToolExecutionError(str(exc)) from exc

    context.broker.publish(
        "edl",
        {
            "project_id": context.project_id,
            "edl_id": result["edl_id"],
            "version": result["version"],
            "aspect_ratio": result["aspect_ratio"],
            "output_asset_id": result["output_asset"]["id"],
            "status": "built",
        },
    )
    return result


# ---------------------------------------------------------------------------
# Handlers M3-B: plantillas, jobs, etiquetado, estado del sistema y export EDL
# ---------------------------------------------------------------------------


async def _handle_list_templates(
    args: ListTemplatesArgs,
    context: ToolContext,
) -> dict[str, Any]:
    registry = get_default_registry()
    templates = registry.list_templates(include_workflow=args.include_workflow)
    return {
        "count": len(templates),
        "include_workflow": args.include_workflow,
        "templates": templates,
    }


async def _handle_job_status(
    args: JobStatusArgs,
    context: ToolContext,
) -> dict[str, Any]:
    if args.job_id:
        job = context.store.get_job(args.job_id)
        if job is None or job.get("project_id") != context.project_id:
            raise ToolExecutionError(
                f"Job no encontrado en el proyecto activo: {args.job_id!r}"
            )
        return {"project_id": context.project_id, "job": job}

    jobs = context.store.list_jobs(project_id=context.project_id)[: args.limit]
    return {
        "project_id": context.project_id,
        "count": len(jobs),
        "jobs": jobs,
    }


async def _handle_job_cancel(
    args: JobCancelArgs,
    context: ToolContext,
) -> dict[str, Any]:
    job = context.store.get_job(args.job_id)
    if job is None or job.get("project_id") != context.project_id:
        raise ToolExecutionError(f"Job no encontrado en el proyecto activo: {args.job_id!r}")
    if job["status"] in ("done", "failed", "error", "cancelled"):
        return {
            "job_id": job["id"],
            "status": job["status"],
            "already_terminal": True,
        }

    was_running = job["status"] == "running"
    interrupt_error: str | None = None
    if was_running:
        try:
            await context.comfyui_client.interrupt()
        except Exception as exc:
            interrupt_error = str(exc)

    updated = context.store.update_job(
        args.job_id,
        status="cancelled",
        error=interrupt_error or "Cancelado por el agente (job_cancel).",
    )
    context.broker.publish(
        "job",
        {
            "job_id": args.job_id,
            "project_id": context.project_id,
            "status": "cancelled",
        },
    )
    return {
        "job_id": args.job_id,
        "status": (updated or {}).get("status", "cancelled"),
        "interrupted_active_run": was_running and interrupt_error is None,
        "interrupt_error": interrupt_error,
    }


async def _handle_tag_asset(
    args: TagAssetArgs,
    context: ToolContext,
) -> dict[str, Any]:
    extra_meta = {"note": args.note} if args.note else None
    updated = context.store.update_asset_metadata(
        context.project_id,
        args.asset_id,
        tags=args.tags,
        metadata=extra_meta,
    )
    if updated is None:
        raise ToolExecutionError(
            f"Asset no encontrado en el proyecto activo: {args.asset_id!r}"
        )
    return {
        "status": "tagged",
        "project_id": context.project_id,
        "asset": updated,
    }


async def _handle_system_status(
    args: SystemStatusArgs,
    context: ToolContext,
) -> dict[str, Any]:
    import os
    from pathlib import Path

    supervisor: dict[str, Any] | None = None
    sup_path = Path(os.environ.get("COMFY_SUPERVISOR_JSON", "/run/comfyui-supervisor.json"))
    try:
        if sup_path.is_file():
            loaded = json.loads(sup_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                supervisor = loaded
    except (OSError, json.JSONDecodeError):
        supervisor = None

    queue_summary: dict[str, Any] | None = None
    try:
        queue = await context.comfyui_client.get_queue()
        queue_summary = {
            "running": len(queue.queue_running),
            "pending": len(queue.queue_pending),
        }
    except Exception:
        queue_summary = None

    jobs = context.store.list_jobs(project_id=context.project_id)
    jobs_by_status: dict[str, int] = {}
    for job in jobs:
        key = str(job.get("status") or "unknown")
        jobs_by_status[key] = jobs_by_status.get(key, 0) + 1

    return {
        "project_id": context.project_id,
        "supervisor": supervisor,
        "comfyui_queue": queue_summary,
        "jobs_by_status": jobs_by_status,
        "total_jobs": len(jobs),
    }


async def _handle_export_edl(
    args: ExportEdlArgs,
    context: ToolContext,
) -> dict[str, Any]:
    edl = (
        context.store.get_edl_version(context.project_id, version=args.version)
        if args.version is not None
        else context.store.get_latest_edl(context.project_id)
    )
    if edl is None:
        raise ToolExecutionError("No hay ninguna versión de EDL en el proyecto activo.")

    output_asset = None
    output_asset_id = edl.get("output_asset_id")
    if output_asset_id:
        output_asset = context.store.get_asset(context.project_id, str(output_asset_id))

    return {
        "status": "exported",
        "project_id": context.project_id,
        "edl_id": edl["id"],
        "version": edl["version"],
        "edl": edl["edl"],
        "output_asset": output_asset,
    }


async def _handle_update_plan(
    args: UpdatePlanArgs,
    context: ToolContext,
) -> dict[str, Any]:
    plan_id = args.plan_id
    if plan_id is None:
        active = context.store.get_active_plan(context.project_id)
        plan_id = active["id"] if active is not None else None

    steps = [step.model_dump() for step in args.steps]
    try:
        if plan_id is not None:
            plan = context.store.save_plan(
                context.project_id,
                title=args.title,
                steps=steps,
                status=args.status,
                plan_id=plan_id,
            )
        else:
            plan = context.store.save_plan(
                context.project_id,
                title=args.title,
                steps=steps,
                status=args.status,
            )
    except (ValueError, LookupError) as exc:
        raise ToolExecutionError(str(exc)) from exc

    context.broker.publish(
        "plan",
        {
            "project_id": context.project_id,
            "plan_id": plan["id"],
            "status": plan["status"],
            "title": plan["title"],
            "steps": plan["steps"],
        },
    )
    return {
        "status": "saved",
        "project_id": context.project_id,
        "plan": plan,
    }


async def _handle_delegate_role(
    args: DelegateRoleArgs,
    context: ToolContext,
) -> dict[str, Any]:
    if context.delegate_runner is None:
        raise ToolExecutionError(
            "La delegación de roles no está disponible en este contexto "
            "(profundidad máxima de sub-agentes alcanzada o delegación deshabilitada)."
        )
    try:
        result = await context.delegate_runner(
            role=args.role,
            task=args.task,
            max_iterations=args.max_iterations,
        )
    except ValueError as exc:
        raise ToolExecutionError(str(exc)) from exc
    return {"role": args.role, "task": args.task, **result}


def create_restricted_belt(base_belt: ToolBelt, role: str) -> ToolBelt:
    """Construye un tool-belt restringido al subconjunto de herramientas de un rol."""
    allowed = ROLE_TOOL_SUBSETS.get(role, ())
    belt = ToolBelt()
    for name in allowed:
        spec = base_belt.get(name)
        if spec is not None:
            belt.register(spec)
    return belt


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
    parallel_safe: bool = True

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
                    **({"$defs": raw_schema["$defs"]} if "$defs" in raw_schema else {}),
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

    def list_specs(self) -> list[ToolSpec]:
        """Devuelve las especificaciones registradas (nombre + descripción) por orden de alta."""
        return list(self._tools.values())

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
                raise ToolExecutionError(f"JSON inválido en argumentos para {name}: {exc}") from exc
        elif isinstance(raw_arguments, dict):
            parsed_args = dict(raw_arguments)
        else:
            raise ToolExecutionError(f"Los argumentos para {name} deben ser un objeto JSON.")

        try:
            validated = spec.args_model.model_validate(parsed_args)
        except ValidationError as exc:
            raise ToolExecutionError(f"Parámetros inválidos para {name}: {exc}") from exc

        return await spec.handler(validated, context)


def create_default_tool_belt() -> ToolBelt:
    """Construye el tool-belt del agente con las herramientas de render, percepción, ASR y EDL."""
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
    belt.register(
        ToolSpec(
            name="extract_keyframes",
            description=(
                "Extrae keyframes de un clip de vídeo del proyecto mediante detección de "
                "escenas e intervalo fijo con ffmpeg, guardando las miniaturas con su timestamp."
            ),
            args_model=ExtractKeyframesArgs,
            handler=_handle_extract_keyframes,
        )
    )
    belt.register(
        ToolSpec(
            name="index_media",
            description=(
                "Indexa visualmente los vídeos e imágenes del proyecto con embeddings CLIP "
                "y detección de rostros InsightFace en la base vectorial sqlite-vec."
            ),
            args_model=IndexMediaArgs,
            handler=_handle_index_media,
        )
    )
    belt.register(
        ToolSpec(
            name="find_shots_text",
            description=(
                "Busca planos y keyframes del proyecto por descripción visual en lenguaje "
                "natural usando similitud coseno sobre embeddings CLIP en sqlite-vec."
            ),
            args_model=FindShotsTextArgs,
            handler=_handle_find_shots_text,
        )
    )
    belt.register(
        ToolSpec(
            name="find_shots_face",
            description=(
                "Busca planos donde aparece una identidad registrada o un rostro de referencia "
                "(con filtro opcional de texto CLIP para búsquedas combinadas texto ∩ persona)."
            ),
            args_model=FindShotsFaceArgs,
            handler=_handle_find_shots_face,
        )
    )
    belt.register(
        ToolSpec(
            name="transcribe",
            description=(
                "Transcribe el audio de un vídeo o archivo de voz con whisperx (faster-whisper "
                "large-v3 + aligners wav2vec2 ES/EN) generando timestamps por palabra."
            ),
            args_model=TranscribeArgs,
            handler=_handle_transcribe,
        )
    )
    belt.register(
        ToolSpec(
            name="diarize",
            description=(
                "Ejecuta diarización de hablantes con FunASR cam++ sin pyannote ni HF_TOKEN "
                "y fusiona los turnos (SPEAKER_00, SPEAKER_01) con la transcripción."
            ),
            args_model=DiarizeArgs,
            handler=_handle_diarize,
        )
    )
    belt.register(
        ToolSpec(
            name="build_edl",
            description=(
                "Ejecuta un EDL determinista con ffmpeg recortando y concatenando los segmentos "
                "en formato 16:9 o 9:16 y registrando el MP4 resultante en outputs del proyecto."
            ),
            args_model=BuildEdlArgs,
            handler=_handle_build_edl,
            parallel_safe=False,
        )
    )
    belt.register(
        ToolSpec(
            name="list_templates",
            description=(
                "Lista las plantillas curadas de render disponibles (vídeo e imagen) con sus "
                "parámetros y defaults; opcionalmente incluye el grafo de workflow completo."
            ),
            args_model=ListTemplatesArgs,
            handler=_handle_list_templates,
        )
    )
    belt.register(
        ToolSpec(
            name="job_status",
            description=(
                "Consulta el estado de un job de render del proyecto activo (o los jobs más "
                "recientes si se omite job_id): queued/running/done/failed/cancelled con progreso."
            ),
            args_model=JobStatusArgs,
            handler=_handle_job_status,
        )
    )
    belt.register(
        ToolSpec(
            name="job_cancel",
            description=(
                "Cancela un job del proyecto activo (best-effort): interrumpe la ejecución activa "
                "de ComfyUI si el job está corriendo y lo marca como cancelado."
            ),
            args_model=JobCancelArgs,
            handler=_handle_job_cancel,
            parallel_safe=False,
        )
    )
    belt.register(
        ToolSpec(
            name="tag_asset",
            description=(
                "Añade etiquetas libres y una nota editorial al metadato de un asset del "
                "proyecto (útil para organizar brutos, tomas buenas y entregables)."
            ),
            args_model=TagAssetArgs,
            handler=_handle_tag_asset,
        )
    )
    belt.register(
        ToolSpec(
            name="system_status",
            description=(
                "Devuelve el estado del sistema: supervisor (si está disponible), cola de "
                "ComfyUI (running/pending) y recuento de jobs del proyecto por estado."
            ),
            args_model=SystemStatusArgs,
            handler=_handle_system_status,
        )
    )
    belt.register(
        ToolSpec(
            name="export_edl",
            description=(
                "Exporta una versión del EDL del proyecto (última por defecto) con el documento "
                "de cortes completo y el asset de salida renderizado con su URL de descarga."
            ),
            args_model=ExportEdlArgs,
            handler=_handle_export_edl,
        )
    )
    belt.register(
        ToolSpec(
            name="update_plan",
            description=(
                "Crea o actualiza el plan de trabajo del turno actual: pasos ordenados con "
                "estado (pending/in_progress/done/skipped) para tareas multi-paso. Úsalo antes "
                "de ejecutar secuencias largas de herramientas."
            ),
            args_model=UpdatePlanArgs,
            handler=_handle_update_plan,
            parallel_safe=False,
        )
    )
    belt.register(
        ToolSpec(
            name="delegate_role",
            description=(
                "Delega una subtarea a un sub-agente especializado con tool-belt restringido: "
                "'director' (renders y parámetros), 'editor' (EDL, transcripción y montaje) o "
                "'artist3d' (assets visuales, indexado y 3D). Profundidad máxima: 1 nivel."
            ),
            args_model=DelegateRoleArgs,
            handler=_handle_delegate_role,
            parallel_safe=False,
        )
    )
    return belt
