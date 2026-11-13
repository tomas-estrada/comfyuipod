"""CPU contracts for bounded, per-shot project reference selection (C23/W08)."""

from __future__ import annotations

from comfy_studio.av_recipes import max_image_references_for
from comfy_studio.orchestration.planner import _sequences_from_raw
from comfy_studio.orchestration.reference_selection import compile_project_references
from comfy_studio.orchestration.state import ProductionState


def _state() -> ProductionState:
    state = ProductionState(idea="reel desde referencias")
    state.sequences = _sequences_from_raw(
        {
            "sequences": [
                {
                    "title": "secuencia",
                    "shots": [
                        {
                            "description": "detalle del perro corriendo en el parque",
                            "layers": [
                                {
                                    "kind": "gen",
                                    "engine": "comfyui",
                                    "params": {"prompt": "perro corriendo"},
                                }
                            ],
                        },
                        {
                            "description": "plano del coche rojo en carretera",
                            "layers": [
                                {
                                    "kind": "gen",
                                    "engine": "comfyui",
                                    "params": {"prompt": "coche rojo"},
                                }
                            ],
                        },
                    ],
                }
            ]
        },
        state.idea,
        vbench_min=0.0,
        semantic_min=0.0,
    )
    return state


def _asset(asset_id: str, *tags: str) -> dict:
    return {
        "id": asset_id,
        "kind": "image",
        "filename": f"{asset_id}.png",
        "original_filename": f"{asset_id}.png",
        "metadata": {"tags": list(tags)},
    }


def test_100_project_assets_compile_bounded_distinct_references_per_shot() -> None:
    assets = [_asset(f"asset-{i:03d}", "fondo sin relación") for i in range(100)]
    assets[23] = _asset("asset-023", "perro", "parque")
    assets[91] = _asset("asset-091", "coche", "carretera")

    state = _state()
    report = compile_project_references(state, assets)

    first, second = state.all_shots()
    first_layer = first.layers[0]
    second_layer = second.layers[0]
    assert first_layer.params["image_asset_id"] == "asset-023"
    assert second_layer.params["image_asset_id"] == "asset-091"
    assert first_layer.params["capability"] == "video.image_to_video"
    assert second_layer.params["capability"] == "video.image_to_video"
    assert first_layer.params["reference_selection"]["capacity"] == 1
    assert first_layer.params["reference_selection"]["candidate_count"] == 100
    assert len(first_layer.params["reference_selection"]["selected"]) == 1
    assert len(first_layer.params["reference_selection"]["excluded"]) == 99
    assert len(second_layer.params["reference_selection"]["excluded"]) == 99
    assert report["shots_compiled"] == 2
    assert report["candidate_assets"] == 100


def test_recipe_reference_capacity_matches_runtime_template_inputs() -> None:
    assert max_image_references_for(
        "video.image_to_video", template_id="wan_i2v"
    ) == 1
    assert max_image_references_for(
        "video.text_to_video", template_id="wan_t2v"
    ) == 0


def test_reference_selection_is_deterministic_and_reports_unmatched_assets() -> None:
    state_a = _state()
    state_b = _state()
    assets = [_asset(f"asset-{i:03d}", "perro") for i in range(4)]

    compile_project_references(state_a, assets)
    compile_project_references(state_b, assets)

    selection_a = state_a.all_shots()[0].layers[0].params["reference_selection"]
    selection_b = state_b.all_shots()[0].layers[0].params["reference_selection"]
    assert selection_a == selection_b
    assert selection_a["selected"][0]["asset_id"] == "asset-000"
    assert [entry["asset_id"] for entry in selection_a["excluded"]] == [
        "asset-001",
        "asset-002",
        "asset-003",
    ]
    assert {entry["reason"] for entry in selection_a["excluded"]} == {
        "model_input_capacity"
    }


def test_explicit_unknown_reference_blocks_and_is_not_silently_dropped() -> None:
    state = _state()
    layer = state.all_shots()[0].layers[0]
    layer.params["reference_asset_ids"] = ["missing-from-project"]

    compile_project_references(state, [_asset("asset-001", "perro")])

    selection = layer.params["reference_selection"]
    assert selection["selected"] == []
    assert selection["unresolved"] == [
        {"asset_id": "missing-from-project", "reason": "not_in_project"}
    ]
    assert layer.params["recipe_unresolved"]
    assert "missing-from-project" in layer.params["recipe_unresolved"]


def test_explicit_reference_over_capacity_uses_best_match_and_explains_rest() -> None:
    state = _state()
    layer = state.all_shots()[0].layers[0]
    layer.params["reference_asset_ids"] = ["asset-1", "asset-2"]
    assets = [_asset("asset-1", "perro"), _asset("asset-2", "sin relación")]

    compile_project_references(state, assets)

    selection = layer.params["reference_selection"]
    assert [entry["asset_id"] for entry in selection["selected"]] == ["asset-1"]
    assert selection["excluded"] == [
        {"asset_id": "asset-2", "reason": "model_input_capacity"}
    ]
    assert layer.params["image_asset_id"] == "asset-1"


def test_explicit_t2v_template_reports_that_it_cannot_consume_image_references() -> None:
    state = _state()
    layer = state.all_shots()[0].layers[0]
    layer.template_id = "wan_t2v"
    layer.params["reference_asset_ids"] = ["asset-1"]

    compile_project_references(state, [_asset("asset-1", "perro")])

    selection = layer.params["reference_selection"]
    assert selection["capacity"] == 0
    assert selection["selected"] == []
    assert selection["excluded"] == [
        {"asset_id": "asset-1", "reason": "model_has_no_image_reference_input"}
    ]
    assert layer.params["recipe_unresolved"]
