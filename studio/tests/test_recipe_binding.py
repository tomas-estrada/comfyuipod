"""Enlace planner → receta AV → plantilla (plan Ultra, W08 integración / A10).

bind_recipes asigna recetas ejecutables a las capas de generación de un plan,
según capacidad y presupuesto de VRAM real. Puro CPU.
"""

from __future__ import annotations

from comfy_studio.orchestration.planner import plan_production
from comfy_studio.orchestration.recipe_binding import bind_recipes
from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
)

VRAM_96 = 94 * 1024
VRAM_32 = 32 * 1024


def _state_with_gen_layer(template_id: str | None = None, params: dict | None = None):
    shot = Shot(description="dron costero", id="s1")
    shot.layers = [
        Layer(kind="gen", engine="comfyui", template_id=template_id, params=params or {})
    ]
    st = ProductionState(idea="reel")
    st.sequences = [Sequence(title="seq", id="seq1", shots=[shot])]
    return st, shot


def test_binds_recipe_to_gen_layer_without_template() -> None:
    st, shot = _state_with_gen_layer()
    summary = bind_recipes(st, vram_budget_mb=VRAM_96)
    assert summary["bound"] == 1
    layer = shot.layers[0]
    assert layer.template_id, "debe quedar con una plantilla ejecutable"
    assert layer.params.get("recipe_id")  # receta trazable
    assert layer.engine in {"ltx25", "wan22"}  # motor AV real


def test_existing_template_is_respected() -> None:
    st, shot = _state_with_gen_layer(template_id="wan_t2v")
    summary = bind_recipes(st, vram_budget_mb=VRAM_96)
    assert summary["skipped_has_template"] == 1
    assert summary["bound"] == 0
    assert shot.layers[0].template_id == "wan_t2v"  # no se toca


def test_unresolvable_layer_is_marked_not_invented() -> None:
    """Si la petición no cabe en ninguna receta, se marca; no se inventa plantilla."""
    st, shot = _state_with_gen_layer(params={"width": 7680, "height": 4320})
    summary = bind_recipes(st, vram_budget_mb=VRAM_96)
    assert summary["unresolved"] == 1
    layer = shot.layers[0]
    assert layer.template_id is None  # NO se inventó una plantilla
    assert "recipe_unresolved" in layer.params


def test_binding_is_idempotent() -> None:
    st, shot = _state_with_gen_layer()
    first = bind_recipes(st, vram_budget_mb=VRAM_96)
    second = bind_recipes(st, vram_budget_mb=VRAM_96)
    assert first["bound"] == 1
    # La segunda pasada ya ve la plantilla puesta: no reasigna.
    assert second["bound"] == 0
    assert second["skipped_has_template"] == 1


def test_small_budget_picks_the_cheaper_recipe_in_binding() -> None:
    st, shot = _state_with_gen_layer(params={"width": 832, "height": 480, "frames": 81})
    bind_recipes(st, vram_budget_mb=VRAM_32)
    assert shot.layers[0].params.get("recipe_id") == "wan22_t2v_480p"


def test_binds_over_a_real_planned_state() -> None:
    """Flujo completo: plan_production (stub) + bind_recipes asigna recetas."""
    state = ProductionState(idea="un dron sobrevuela una ciudad costera", cost_cap_usd=1.0)
    plan_production(state)  # stub planner: crea shots con capas gen sin plantilla
    # Antes del binding, las capas gen pueden no tener plantilla ejecutable.
    summary = bind_recipes(state, vram_budget_mb=VRAM_96)
    # Al menos una capa de generación quedó enlazada a una receta real.
    assert summary["bound"] >= 1
    gen_layers = [
        ly for sh in state.all_shots() for ly in sh.layers if ly.kind == "gen"
    ]
    bound = [ly for ly in gen_layers if ly.params.get("recipe_id")]
    assert bound, "al menos una capa gen quedó con receta ejecutable"
