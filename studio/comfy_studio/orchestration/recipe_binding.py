"""Enlace planner → receta AV → plantilla ejecutable (plan Ultra, W08 integración).

El planner (fase 13) descompone la idea en shots y capas, pero una capa de
generación sin `template_id` no es ejecutable: el `ComfyUIEngine` necesita un
grafo (lección aprendida en M6). `av_recipes` (W08) sabe elegir la receta viable
según capacidad y presupuesto de VRAM real; este módulo conecta ambas cosas sin
acoplar el planner al catálogo.

`bind_recipes(state, vram_budget_mb)` recorre los shots ya planificados y, para
cada capa de generación (`gen`) de un motor AV sin plantilla, selecciona la
receta de mayor fidelidad que cabe en el presupuesto y fija su `template_id` y
el motor real. Una capa que ya trae plantilla se respeta. Si ninguna receta cabe,
la capa queda marcada con el motivo (no se inventa una plantilla): el shot lo
tratará el feedback loop.

Puro cálculo determinista (reutiliza el selector CPU de W08). No ejecuta GPU.
"""

from __future__ import annotations

from comfy_studio.av_recipes import VRAM_SAFETY_FACTOR, recipes_for, select_recipe
from comfy_studio.orchestration.state import ProductionState

#: capas de generación por capacidad: qué pedir al selector según el kind/params.
_GEN_KINDS = {"gen", "generate", "t2v", "i2v", "image"}

#: resolución/frames por defecto si el shot no los declara (caso nominal).
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FRAMES = 81


def _capability_for(
    layer_kind: str, params: dict, template_id: str | None = None
) -> str:
    """Capacidad AV que una capa de generación representa."""
    explicit = params.get("capability")
    if explicit:
        return str(explicit)
    template_capability = {
        "wan_i2v": "video.image_to_video",
        "wan_t2v": "video.text_to_video",
        "sdxl_image": "image.generate",
    }.get(template_id)
    if template_capability:
        return template_capability
    if layer_kind in ("image", "generate_image"):
        return "image.generate"
    if layer_kind in ("i2v",):
        return "video.image_to_video"
    return "video.text_to_video"


def bind_recipes(
    state: ProductionState,
    *,
    vram_budget_mb: int,
) -> dict[str, int]:
    """Asigna recetas AV ejecutables a las capas de generación sin plantilla.

    Devuelve un resumen {bound, skipped_has_template, unresolved}. Idempotente:
    una segunda pasada no reasigna lo ya resuelto.
    """
    summary = {"bound": 0, "skipped_has_template": 0, "unresolved": 0}
    for shot in state.all_shots():
        for layer in shot.layers:
            if layer.kind not in _GEN_KINDS:
                continue
            capability = _capability_for(layer.kind, layer.params, layer.template_id)
            width = int(layer.params.get("width", DEFAULT_WIDTH))
            height = int(layer.params.get("height", DEFAULT_HEIGHT))
            frames = int(layer.params.get("frames", DEFAULT_FRAMES))
            if layer.template_id:
                # Keep the planner's explicit template, but still admit it
                # against a catalog recipe when one exists. A template without
                # a recipe estimate is unresolved for a resource-gated run.
                matching = [
                    recipe
                    for recipe in recipes_for(capability)
                    if recipe.template_id == layer.template_id
                ]
                if not matching:
                    layer.params["recipe_unresolved"] = (
                        f"sin receta de recursos para plantilla {layer.template_id}"
                    )
                    summary["unresolved"] += 1
                    continue
                selected = next(
                    (
                        recipe
                        for recipe in matching
                        if recipe.fits_request(width=width, height=height, frames=frames)[0]
                        and recipe.fits_budget(vram_budget_mb)[0]
                        and (not layer.engine or recipe.engine == layer.engine)
                    ),
                    None,
                )
                if selected is None:
                    selected = next(
                        (
                            recipe
                            for recipe in matching
                            if recipe.fits_request(width=width, height=height, frames=frames)[0]
                            and recipe.fits_budget(vram_budget_mb)[0]
                        ),
                        None,
                    )
                if selected is None:
                    layer.params["recipe_unresolved"] = (
                        "la plantilla explícita excede límites de receta o presupuesto VRAM"
                    )
                    summary["unresolved"] += 1
                    continue
                layer.params["recipe_id"] = selected.recipe_id
                layer.params["lease_vram_mb"] = int(
                    selected.est_vram_mb * VRAM_SAFETY_FACTOR
                )
                summary["skipped_has_template"] += 1
                continue
            result = select_recipe(
                capability,
                width=width,
                height=height,
                frames=frames,
                vram_budget_mb=vram_budget_mb,
            )
            if result.selected is None:
                layer.params["recipe_unresolved"] = result.reason
                summary["unresolved"] += 1
                continue
            recipe = result.selected
            layer.template_id = recipe.template_id
            layer.engine = recipe.engine
            layer.params["recipe_id"] = recipe.recipe_id
            # Carry the same conservative estimate used at selection into the
            # runtime lease admission. This is still an estimate, not a GPU
            # measurement; the lease separately caps it to detected hardware.
            layer.params["lease_vram_mb"] = int(recipe.est_vram_mb * VRAM_SAFETY_FACTOR)
            layer.params.setdefault("width", width)
            layer.params.setdefault("height", height)
            layer.params.setdefault("frames", frames)
            summary["bound"] += 1
    if summary["bound"] or summary["unresolved"]:
        state.record(
            "bind_recipes",
            bound=summary["bound"],
            unresolved=summary["unresolved"],
            vram_budget_mb=vram_budget_mb,
        )
    return summary


__all__ = ["bind_recipes"]
