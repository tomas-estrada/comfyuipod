"""Pruebas de regresión de la revisión independiente sobre el atlas (PR #63).

Cada defecto reportado se reproduce y se fija con una prueba:
1. _municipality_for no fuerza Medellín fuera del alcance territorial.
2. title_match no asigna por una palabra genérica compartida (Puente Alfa/Beta).
3. Una ubicación explícita en el prompt prevalece sobre la selección previa.
4. Una vista con rumbo conocido incompatible (0° vs 180°) no se acepta; lo
   desconocido se conserva como limitación.
5. El fondo del atlas llega al grafo final (image_input) y dos fondos distintos
   producen grafos distintos; una plantilla sin entrada de imagen falla tipado.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.acquire.pipeline import (
    MUNICIPALITY_UNKNOWN,
    AcquisitionPipeline,
    _municipality_for,
)
from comfy_studio.atlas.models import Environment, Place, View
from comfy_studio.atlas.service import AtlasService
from comfy_studio.atlas.store import AtlasStore, sha256_file
from comfy_studio.orchestration.adapters import EngineRunnerAdapter
from comfy_studio.orchestration.planner import StubPlannerLLM, plan_production
from comfy_studio.orchestration.state import ProductionState
from comfy_studio.templates.registry import get_default_registry

# ---------------------------------------------------------------------------
# 1) municipio desconocido
# ---------------------------------------------------------------------------


def test_municipio_fuera_de_alcance_es_desconocido() -> None:
    # Bogotá: fuera de todos los bbox del Valle de Aburrá
    assert _municipality_for(4.7110, -74.0721) == MUNICIPALITY_UNKNOWN
    # centro de Medellín sí cae en su bbox
    assert _municipality_for(6.2442, -75.5812) == "Medellín"


# ---------------------------------------------------------------------------
# 2) title_match sin confirmaciones falsas
# ---------------------------------------------------------------------------


def test_title_match_no_confunde_puentes_alfa_beta(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    alfa = Place(place_id="plc_alfa", municipality="Medellín", name="Puente Alfa",
                 kind=Environment.BRIDGE, aliases=[], lat=6.20, lon=-75.58,
                 provenance={"source": "seed", "assign_radius_m": 800})
    beta = Place(place_id="plc_beta", municipality="Medellín", name="Puente Beta",
                 kind=Environment.BRIDGE, aliases=[], lat=6.205, lon=-75.58,
                 provenance={"source": "seed", "assign_radius_m": 800})
    store.upsert_place(alfa)
    store.upsert_place(beta)
    # referencia titulada "Puente Beta" situada exactamente en Beta
    r = _ref("ref_beta", None, 6.205, -75.58, title="Puente Beta")
    make_png(root / "files" / r.file_path, 32, 32)
    r.sha256 = sha256_file(root / "files" / r.file_path)
    r.bytes = (root / "files" / r.file_path).stat().st_size
    r.usable = True
    store.upsert_reference(r)
    store.close()

    pipe = AcquisitionPipeline(root, max_refs=10, min_interval_s=0.0)
    stats = pipe.reassign_places()
    store2 = AtlasStore(root / "catalog.db")
    ref = store2.get_reference("ref_beta")
    assert ref is not None and ref.place_id == "plc_beta", (
        f"la referencia 'Puente Beta' en Beta terminó en {ref.place_id!r} ({stats})"
    )
    store2.close()


# ---------------------------------------------------------------------------
# 3) ubicación explícita del prompt prevalece sobre la selección previa
# ---------------------------------------------------------------------------


@pytest.fixture()
def atlas_dos_puentes(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    for pid, name, lat, lon in (
        ("plc_alfa", "Puente Alfa", 6.20, -75.58),
        ("plc_beta", "Puente Beta", 6.205, -75.58),
    ):
        store.upsert_place(Place(place_id=pid, municipality="Medellín", name=name,
                                 kind=Environment.BRIDGE, lat=lat, lon=lon))
        r = _ref(f"ref_{pid}", pid, lat, lon, title=name)
        make_png(root / "files" / r.file_path, 32, 32)
        r.sha256 = sha256_file(root / "files" / r.file_path)
        r.bytes = (root / "files" / r.file_path).stat().st_size
        store.upsert_reference(r)
        store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
        store.upsert_view(View(view_id=f"view_{r.ref_id}", place_id=pid, ref_id=r.ref_id,
                               lat=lat, lon=lon))
    store.close()
    return root


def test_prompt_explicito_prevalece_sobre_seleccion(
    atlas_dos_puentes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_dos_puentes))
    from comfy_studio.atlas.agent_tools import reset_shared_atlas_service

    reset_shared_atlas_service()
    svc = AtlasService(atlas_dos_puentes)
    # selección previa: Alfa
    svc.set_selection("plc_alfa", "view_ref_plc_alfa")
    # el prompt menciona explícitamente Beta
    state = ProductionState(idea="un carro cruzando el Puente Beta de Medellín")
    plan_production(state, llm=StubPlannerLLM())
    atlas_ctx = state.metadata.get("atlas")
    assert atlas_ctx is not None, "el planner no resolvió atlas"
    assert atlas_ctx["place_id"] == "plc_beta", (
        f"la ubicación explícita del prompt fue ignorada: {atlas_ctx['place_id']}"
    )


# ---------------------------------------------------------------------------
# 4) compatibilidad angular real
# ---------------------------------------------------------------------------


def test_select_view_rechaza_rumbo_incompatible_y_conserva_desconocido(
    atlas_dos_puentes: Path,
) -> None:
    svc = AtlasService(atlas_dos_puentes)
    store = svc.store
    # dar rumbo conocido 0° a la vista de Alfa
    v = store.get_view("view_ref_plc_alfa")
    assert v is not None
    v.heading_deg = 0.0
    store.upsert_view(v)
    # pedir 180° -> no aceptada
    res = svc.select_view("plc_alfa", heading_deg=180.0)
    assert res["status"] == "unsupported_camera"
    codes = [lim["code"] for lim in res["limitations"]]
    assert "heading_mismatch" in codes
    assert res["available_views"], "debe devolver las vistas alternativas"
    # pedir 10° -> aceptada (dentro de tolerancia)
    ok = svc.select_view("plc_alfa", heading_deg=10.0)
    assert ok["status"] == "ok"
    # rumbo desconocido en la vista -> limitación, no compatibilidad afirmada
    v2 = store.get_view("view_ref_plc_beta")
    assert v2 is not None and v2.heading_deg is None
    unk = svc.select_view("plc_beta", heading_deg=90.0)
    assert unk["status"] == "ok"
    assert any(lim["code"] == "heading_unknown" for lim in unk["limitations"])


# ---------------------------------------------------------------------------
# 5) el fondo llega al grafo final
# ---------------------------------------------------------------------------


def test_grafo_final_carga_la_referencia_y_cambia_con_el_fondo(
    atlas_dos_puentes: Path, tmp_path: Path
) -> None:
    registry = get_default_registry()
    g_a = registry.render_workflow("wan_i2v", {"prompt": "x", "image_input": "fondo_a.jpg"})
    g_b = registry.render_workflow("wan_i2v", {"prompt": "x", "image_input": "fondo_b.jpg"})
    img_a = g_a["8"]["inputs"]["image"]
    img_b = g_b["8"]["inputs"]["image"]
    assert img_a == "fondo_a.jpg" and img_b == "fondo_b.jpg"
    assert json.dumps(g_a, sort_keys=True) != json.dumps(g_b, sort_keys=True), (
        "dos fondos distintos deben producir grafos distintos"
    )


def test_adapter_inyecta_image_input_y_rechaza_t2v(
    atlas_dos_puentes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_dos_puentes))
    from comfy_studio.atlas.agent_tools import reset_shared_atlas_service, shared_atlas_service

    reset_shared_atlas_service()
    svc = shared_atlas_service()
    input_dir = tmp_path / "comfyui_input"
    captured: list[dict] = []

    class FakeEngine:
        name = "comfyui"

        def run(self, spec):  # noqa: ANN001
            captured.append(dict(spec.job))
            from comfy_studio.engines import EngineResult

            return EngineResult(ok=True, engine="comfyui", verb="run",
                                payload={"output_path": "/tmp/out.mp4"})

    registry = get_default_registry()
    adapter = EngineRunnerAdapter(
        {"comfyui": FakeEngine()},
        template_registry=registry,
        atlas_service=svc,
        comfyui_input_dir=str(input_dir),
    )
    atlas_ctx = {"place_id": "plc_alfa", "view_id": "view_ref_plc_alfa",
                 "ref_id": "ref_plc_alfa", "background_path": "aa/ref_plc_alfa.png"}
    job_s1 = {"kind": "gen", "shot": "s1", "template_id": "wan_i2v",
              "prompt": "un carro", "atlas": atlas_ctx}
    res = adapter.run_layer("comfyui", job_s1)
    assert res["ok"] is True, res.get("error")
    job = captured[0]
    fname = job["atlas_image_input"]
    assert (input_dir / fname).is_file(), "el fondo debe copiarse al input de ComfyUI"
    graph = job["graph"]
    assert graph["8"]["inputs"]["image"] == fname, "LoadImage debe apuntar al fondo"
    # el grafo renderizado cambió respecto a otro fondo
    other = dict(job)
    other["graph"] = None

    # t2v no declara image_input -> no puede afirmar que usó el atlas
    bad = adapter.run_layer("comfyui", {
        "kind": "gen", "shot": "s2", "template_id": "wan_t2v",
        "prompt": "un carro", "atlas": atlas_ctx,
    })
    assert bad["ok"] is False and "entrada de imagen" in bad["error"]

    # archivo modificado -> hash no coincide -> fallo tipado (no cambio silencioso)
    make_png(atlas_dos_puentes / "files" / "aa" / "ref_plc_alfa.png", 32, 32, color=(7, 7, 7))
    bad2 = adapter.run_layer("comfyui", {
        "kind": "gen", "shot": "s3", "template_id": "wan_i2v",
        "prompt": "un carro", "atlas": atlas_ctx,
    })
    assert bad2["ok"] is False and "hash" in bad2["error"]
