"""Project assets flow through planning into bounded, auditable I2V layers."""

from __future__ import annotations

from typing import Any

from comfy_studio.orchestration import Collaborators, ProductionCheckpointer, ProductionService
from comfy_studio.projects import ProjectStore


class _ReferenceAwarePlanner:
    def __init__(self) -> None:
        self.context: dict[str, Any] | None = None

    def plan(self, idea: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        self.context = context
        return {
            "sequences": [
                {
                    "title": "Anuncio",
                    "shots": [
                        {
                            "description": "detalle del perro corriendo en el parque",
                            "layers": [{
                                "kind": "gen",
                                "engine": "comfyui",
                                "params": {"prompt": "perro corriendo"},
                            }],
                        },
                        {
                            "description": "plano del coche rojo en carretera",
                            "layers": [{
                                "kind": "gen",
                                "engine": "comfyui",
                                "params": {"prompt": "coche rojo"},
                            }],
                        },
                    ],
                }
            ]
        }


def _save_image(store: ProjectStore, project_id: str, index: int, *tags: str) -> dict[str, Any]:
    return store.save_upload(
        project_id,
        filename=f"asset-{index:03d}.png",
        content_type="image/png",
        data=f"image-fixture-{index}".encode(),
        max_upload_bytes=1024,
        metadata={"tags": list(tags)},
    )


def test_production_service_passes_project_catalog_and_compiles_per_shot_refs(tmp_path) -> None:
    store = ProjectStore(tmp_path / "studio")
    project = store.create_project("Assets")
    assets = [_save_image(store, project["id"], i, "fondo sin relación") for i in range(100)]
    assets[23] = store.update_asset_metadata(
        project["id"], assets[23]["id"], tags=["perro", "parque"]
    )
    assets[91] = store.update_asset_metadata(
        project["id"], assets[91]["id"], tags=["coche", "carretera"]
    )
    planner = _ReferenceAwarePlanner()
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "production.sqlite3"),
        collaborators=Collaborators(planner=planner),
        store=store,
        resource_vram_budget_mb=96 * 1024,
    )

    result = service.plan_production(
        "reel desde referencias", project_id=project["id"]
    )

    assert planner.context is not None
    catalog = planner.context["project_reference_catalog"]
    assert len(catalog) == 100
    assert catalog[23]["asset_id"] == assets[23]["id"]
    plan = result["plan"]
    shots = [shot for sequence in plan["sequences"] for shot in sequence["shots"]]
    first_layer, second_layer = shots[0]["layers"][0], shots[1]["layers"][0]
    assert first_layer["params"]["image_asset_id"] == assets[23]["id"]
    assert second_layer["params"]["image_asset_id"] == assets[91]["id"]
    assert first_layer["template_id"] == second_layer["template_id"] == "wan_i2v"
    assert first_layer["params"]["recipe_id"] == "wan22_i2v_720p"
    assert first_layer["params"]["reference_selection"]["candidate_count"] == 100
    assert plan["metadata"]["project_reference_selection"] == {
        "shots_compiled": 2,
        "candidate_assets": 100,
        "selected": 2,
        "excluded": 198,
        "unresolved": 0,
    }


def test_empty_project_catalog_does_not_silently_drop_explicit_reference(tmp_path) -> None:
    class _MissingReferencePlanner:
        def plan(self, idea: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
            return {
                "sequences": [
                    {
                        "title": "Shot",
                        "shots": [
                            {
                                "description": "perro en el parque",
                                "layers": [{
                                    "kind": "gen",
                                    "engine": "comfyui",
                                    "params": {
                                        "prompt": "perro",
                                        "reference_asset_ids": ["absent-asset"],
                                    },
                                }],
                            }
                        ],
                    }
                ]
            }

    store = ProjectStore(tmp_path / "studio")
    project = store.create_project("Empty assets")
    service = ProductionService(
        checkpointer=ProductionCheckpointer(db_path=tmp_path / "empty.sqlite3"),
        collaborators=Collaborators(planner=_MissingReferencePlanner()),
        store=store,
    )

    plan = service.plan_production("usa la referencia ausente", project_id=project["id"])[
        "plan"
    ]
    layer = plan["sequences"][0]["shots"][0]["layers"][0]

    assert layer["params"]["reference_selection"]["unresolved"] == [
        {"asset_id": "absent-asset", "reason": "not_in_project"}
    ]
    assert layer["params"]["recipe_unresolved"]
    assert plan["metadata"]["project_reference_selection"]["unresolved"] == 1
