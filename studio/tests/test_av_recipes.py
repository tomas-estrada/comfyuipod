"""Recetas de generación AV y selección por recursos (W08 / A10).

Prueban el CONTRATO (selección por capacidad/límites/presupuesto), no calidad ni
kernels CUDA (invariante A10). Puro CPU.
"""

from __future__ import annotations

from comfy_studio.av_recipes import (
    catalog,
    recipes_for,
    select_recipe,
)

VRAM_96GB_USABLE = 94 * 1024  # 96 GB menos reserva, en MB
# 32 GB: Wan 480p (24000*1.15=27600) cabe; LTX 720p (40000*1.15=46000) no.
VRAM_32GB = 32 * 1024


def test_catalog_lists_recipes_with_declared_limits() -> None:
    cat = catalog()
    assert len(cat) >= 5
    for r in cat:
        assert r["max_width"] > 0 and r["max_frames"] >= 1
        # La VRAM estimada se marca explícitamente como NO medida (A10).
        assert "NO medición" in r["est_vram_note"]


def test_only_workflow_and_asset_verified_recipes_are_runtime_supported() -> None:
    supported = {r["recipe_id"] for r in catalog() if r["runtime_supported"]}
    assert supported == {"wan22_t2v_480p", "wan22_i2v_720p"}


def test_recipes_for_capability_sorted_by_vram() -> None:
    t2v = recipes_for("video.text_to_video")
    assert len(t2v) >= 2
    vrams = [r.est_vram_mb for r in t2v]
    assert vrams == sorted(vrams), "deben venir de menor a mayor VRAM"


def test_unimplemented_recipe_families_are_not_selectable() -> None:
    """Large VRAM must not make a mismatched, unimplemented workflow runnable."""
    res = select_recipe(
        "video.text_to_video",
        width=1280,
        height=720,
        frames=81,
        vram_budget_mb=VRAM_96GB_USABLE,
    )
    assert res.selected is None
    assert any(r["recipe"] == "ltx25_t2v_720p" for r in res.rejected)


def test_selects_only_verified_wan_recipe_that_fits() -> None:
    res = select_recipe(
        "video.text_to_video",
        width=832,
        height=480,
        frames=81,
        vram_budget_mb=VRAM_96GB_USABLE,
    )
    assert res.selected is not None
    assert res.selected.recipe_id == "wan22_t2v_480p"


def test_small_budget_falls_back_to_the_cheaper_recipe() -> None:
    """Con 32 GB y 480p, se elige el workflow Wan verificado."""
    res = select_recipe(
        "video.text_to_video",
        width=832,
        height=480,
        frames=81,
        vram_budget_mb=VRAM_32GB,
    )
    assert res.selected is not None
    assert res.selected.recipe_id == "wan22_t2v_480p"
    # LTX no puede seleccionarse hasta que tenga su propio workflow y assets.
    assert any("ltx25" in r["recipe"] for r in res.rejected)


def test_request_exceeding_all_model_limits_selects_nothing() -> None:
    """Pedir 8K rechaza todas las recetas t2v por límite de modelo."""
    res = select_recipe(
        "video.text_to_video",
        width=7680,
        height=4320,
        frames=81,
        vram_budget_mb=VRAM_96GB_USABLE,
    )
    assert res.selected is None
    assert "ninguna receta" in res.reason
    assert len(res.rejected) >= 2


def test_budget_too_small_selects_nothing_without_inventing() -> None:
    """Con un presupuesto ridículo no se inventa una receta: se da el motivo."""
    res = select_recipe(
        "video.text_to_video",
        width=832,
        height=480,
        frames=81,
        vram_budget_mb=1024,  # 1 GB
    )
    assert res.selected is None
    assert res.rejected  # todas rechazadas por VRAM


def test_prefer_honoured_when_viable() -> None:
    res = select_recipe(
        "video.text_to_video",
        width=832,
        height=480,
        frames=81,
        vram_budget_mb=VRAM_96GB_USABLE,
        prefer="wan22_t2v_480p",
    )
    assert res.selected is not None
    assert res.selected.recipe_id == "wan22_t2v_480p"
    assert "preferida" in res.reason


def test_upscale_recipes_target_4k() -> None:
    res = select_recipe(
        "video.upscale",
        width=3840,
        height=2160,
        frames=81,
        vram_budget_mb=VRAM_96GB_USABLE,
    )
    assert res.selected is None
    assert {r["recipe"] for r in res.rejected} == {
        "seedvr2_upscale_4k",
        "flashvsr_upscale_4k",
    }


def test_unknown_capability_returns_no_recipe_with_reason() -> None:
    res = select_recipe(
        "video.does_not_exist",
        width=100,
        height=100,
        frames=1,
        vram_budget_mb=VRAM_96GB_USABLE,
    )
    assert res.selected is None
    assert "capacidad sin recetas" in res.reason
