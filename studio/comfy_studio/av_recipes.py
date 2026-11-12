"""Recetas de generación audiovisual base y selección por recursos (W08 / A10).

W08 pide adapters LTX-2.5 / Wan / QwenImage y finishing FlashVSR+/SeedVR2, con
«manifests por receta/encoder/LoRA/VAE con límites del modelo» y «selección por
capacidades/recursos». El invariante de A10 es explícito: **los tests CPU prueban
contratos, no calidad ni compatibilidad CUDA**.

Este módulo es ese contrato: un catálogo de recetas de generación, cada una con
sus límites declarados (resolución y frames máximos, VRAM estimada, componentes:
encoder/VAE/LoRA, y la plantilla ComfyUI que la ejecuta), y un selector que, dada
una capacidad y un presupuesto de VRAM real (del perfil efectivo de W02), elige
la receta **viable** y rechaza la que no cabe — sin ejecutar nada en GPU.

Los valores de VRAM estimada son **estimaciones declaradas**, no mediciones: la
medición real la hace el render y la publica `recipe_for_template` (W02). Aquí se
usan para filtrar candidatas antes de gastar GPU; nunca se presentan como
medidas. La ejecución real la hace el `ComfyUIEngine` con la plantilla, bajo el
lease de W03.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Margen de seguridad sobre la VRAM estimada al comprobar viabilidad: una
#: estimación no es una medida, así que no se apura al 100% del presupuesto.
VRAM_SAFETY_FACTOR = 1.15


@dataclass(frozen=True, slots=True)
class AVRecipe:
    """Una receta de generación AV con sus límites declarados."""

    recipe_id: str
    capability: str
    engine: str
    template_id: str
    #: límites del modelo (declarados por el engine/doc, no medidos aquí).
    max_width: int
    max_height: int
    max_frames: int
    #: VRAM estimada (MB) para el caso nominal; estimación, no medición.
    est_vram_mb: int
    #: Solo True cuando existen en el repo un workflow, assets y adapter verificados.
    runtime_supported: bool = False
    #: componentes que la receta necesita (para el resolvedor de assets, W01).
    components: dict[str, str] = field(default_factory=dict)
    notes: str = ""

    def fits_request(
        self, *, width: int, height: int, frames: int
    ) -> tuple[bool, str | None]:
        """¿La petición respeta los límites declarados del modelo?"""
        if width > self.max_width or height > self.max_height:
            return False, (
                f"resolución {width}x{height} excede el máximo "
                f"{self.max_width}x{self.max_height} de {self.recipe_id}"
            )
        if frames > self.max_frames:
            return False, (
                f"{frames} frames excede el máximo {self.max_frames} de {self.recipe_id}"
            )
        return True, None

    def fits_budget(self, vram_budget_mb: int) -> tuple[bool, str | None]:
        """¿Cabe la VRAM estimada (con margen) en el presupuesto real?"""
        needed = int(self.est_vram_mb * VRAM_SAFETY_FACTOR)
        if needed > vram_budget_mb:
            return False, (
                f"VRAM estimada {needed} MB (con margen) > presupuesto "
                f"{vram_budget_mb} MB para {self.recipe_id}"
            )
        return True, None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "recipe_id": self.recipe_id,
            "capability": self.capability,
            "engine": self.engine,
            "template_id": self.template_id,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_frames": self.max_frames,
            "est_vram_mb": self.est_vram_mb,
            "runtime_supported": self.runtime_supported,
            "est_vram_note": "estimación declarada, NO medición (A10)",
            "components": dict(self.components),
            "notes": self.notes,
        }


#: Catálogo base de recetas AV. Los engines (ltx25, wan22, qwen-image21,
#: flashvsr-plus, seedvr2) están declarados en engines.json como `required`.
#: Los límites y la VRAM estimada son declarados por receta; se MIDEN en GPU
#: (A29) y la medición real reemplaza la estimación en la selección.
_RECIPES: tuple[AVRecipe, ...] = (
    AVRecipe(
        recipe_id="ltx25_t2v_720p",
        capability="video.text_to_video",
        engine="ltx25",
        template_id="wan_t2v",  # plantilla ComfyUI existente que ejecuta t2v
        max_width=1280,
        max_height=720,
        max_frames=257,
        est_vram_mb=40000,
        components={"vae": "ltx_vae", "encoder": "t5_text_encoder"},
        notes="LTX-2.5 base texto->video; DFR/detailing por receta aparte.",
    ),
    AVRecipe(
        recipe_id="wan22_t2v_480p",
        capability="video.text_to_video",
        engine="wan22",
        template_id="wan_t2v",
        max_width=832,
        max_height=480,
        max_frames=81,
        est_vram_mb=24000,
        runtime_supported=True,
        components={"vae": "wan_2.1_vae", "encoder": "umt5_xxl"},
        notes="Wan 2.2 control especifico; menor VRAM, menor resolucion.",
    ),
    AVRecipe(
        recipe_id="wan22_i2v_720p",
        capability="video.image_to_video",
        engine="wan22",
        template_id="wan_i2v",
        max_width=1280,
        max_height=720,
        max_frames=81,
        est_vram_mb=36000,
        runtime_supported=True,
        components={"vae": "wan_2.1_vae", "encoder": "umt5_xxl"},
        notes="Wan 2.2 imagen->video.",
    ),
    AVRecipe(
        recipe_id="qwen_image_1024",
        capability="image.generate",
        engine="qwen-image21",
        template_id="sdxl_image",
        max_width=1024,
        max_height=1024,
        max_frames=1,
        est_vram_mb=18000,
        components={"vae": "sdxl_vae"},
        notes="QwenImage 2.1 generacion de imagen base.",
    ),
    AVRecipe(
        recipe_id="seedvr2_upscale_4k",
        capability="video.upscale",
        engine="seedvr2",
        template_id="wan_t2v",  # finishing aplicado sobre un clip existente
        max_width=3840,
        max_height=2160,
        max_frames=257,
        est_vram_mb=48000,
        components={},
        notes="SeedVR2 upscale/finishing a 4K; se aplica sobre un clip ya generado.",
    ),
    AVRecipe(
        recipe_id="flashvsr_upscale_4k",
        capability="video.upscale",
        engine="flashvsr-plus",
        template_id="wan_t2v",
        max_width=3840,
        max_height=2160,
        max_frames=257,
        est_vram_mb=42000,
        components={},
        notes="FlashVSR+ finishing temporal a 4K; alternativa a SeedVR2.",
    ),
)


def recipes_for(capability: str) -> list[AVRecipe]:
    """Recetas que ofrecen una capacidad, ordenadas de menor a mayor VRAM."""
    matches = [r for r in _RECIPES if r.capability == capability]
    return sorted(matches, key=lambda r: r.est_vram_mb)


@dataclass(slots=True)
class SelectionResult:
    """Resultado de seleccionar una receta para una petición y un presupuesto."""

    selected: AVRecipe | None
    considered: list[str]
    rejected: list[dict[str, str]]
    reason: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.to_public_dict() if self.selected else None,
            "considered": list(self.considered),
            "rejected": list(self.rejected),
            "reason": self.reason,
        }


def select_recipe(
    capability: str,
    *,
    width: int,
    height: int,
    frames: int,
    vram_budget_mb: int,
    prefer: str | None = None,
) -> SelectionResult:
    """Elige la receta viable para la petición dentro del presupuesto de VRAM.

    Estrategia: entre las que ofrecen la capacidad y respetan los límites del
    modelo Y caben en el presupuesto, se prefiere la de MAYOR VRAM estimada que
    aún cabe (más fidelidad usando el hardware disponible), o la nombrada en
    `prefer`. Si ninguna cabe, no se inventa una: se devuelve el motivo.
    """
    candidates = recipes_for(capability)
    considered = [r.recipe_id for r in candidates]
    rejected: list[dict[str, str]] = []
    viable: list[AVRecipe] = []

    for recipe in candidates:
        if not recipe.runtime_supported:
            rejected.append(
                {
                    "recipe": recipe.recipe_id,
                    "reason": "workflow, assets o adapter runtime no implementados/verificados",
                }
            )
            continue
        ok_req, why_req = recipe.fits_request(width=width, height=height, frames=frames)
        if not ok_req:
            rejected.append({"recipe": recipe.recipe_id, "reason": why_req or "limite"})
            continue
        ok_bud, why_bud = recipe.fits_budget(vram_budget_mb)
        if not ok_bud:
            rejected.append({"recipe": recipe.recipe_id, "reason": why_bud or "vram"})
            continue
        viable.append(recipe)

    if not candidates:
        return SelectionResult(None, considered, rejected, f"capacidad sin recetas: {capability}")
    if not viable:
        return SelectionResult(
            None, considered, rejected, "ninguna receta cabe en la peticion/presupuesto"
        )

    if prefer:
        preferred = next((r for r in viable if r.recipe_id == prefer), None)
        if preferred is not None:
            return SelectionResult(preferred, considered, rejected, f"preferida: {prefer}")

    # Mayor fidelidad que aún cabe (mayor VRAM estimada entre las viables).
    chosen = max(viable, key=lambda r: r.est_vram_mb)
    return SelectionResult(chosen, considered, rejected, "mayor fidelidad viable en presupuesto")


def catalog() -> list[dict[str, Any]]:
    """Catálogo completo de recetas AV (para API/agente/diagnóstico)."""
    return [r.to_public_dict() for r in _RECIPES]


def recipe_by_id(recipe_id: str) -> AVRecipe | None:
    """Busca una receta por identidad estable, incluso si no está implementada."""
    return next((recipe for recipe in _RECIPES if recipe.recipe_id == recipe_id), None)


__all__ = [
    "AVRecipe",
    "SelectionResult",
    "catalog",
    "recipes_for",
    "recipe_by_id",
    "select_recipe",
]
