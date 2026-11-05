"""Tests de integración del atlas: API HTTP, herramientas del agente, planner y
ejecutor (camino vertical planner -> herramienta -> archivo local -> fondo real).

usan un atlas sintético en tmp vía STUDIO_ATLAS_DIR (sin red, sin GPU).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_atlas_core import _ref, make_png  # noqa: F401  (helpers compartidos)

from comfy_studio.app import create_app
from comfy_studio.atlas.agent_tools import (
    AtlasPrepareShotArgs,
    handle_atlas_prepare_shot,
    reset_shared_atlas_service,
    shared_atlas_service,
)
from comfy_studio.atlas.models import Coverage, Environment, Place, View
from comfy_studio.atlas.store import AtlasStore, sha256_file
from comfy_studio.config import StudioConfig
from comfy_studio.orchestration.adapters import EngineRunnerAdapter
from comfy_studio.orchestration.planner import StubPlannerLLM, plan_production
from comfy_studio.orchestration.state import ProductionState
from comfy_studio.stubs.comfyui import ComfyUIStubState, create_stub_comfyui_app


@pytest.fixture()
def atlas_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(
        place_id="plc_medellin_puente_4sur", municipality="Medellín",
        name="Puente de la 4 Sur", kind=Environment.BRIDGE,
        aliases=["puente 4 sur"], lat=6.2088, lon=-75.5670,
    ))
    store.upsert_place(Place(
        place_id="plc_medellin_plaza_botero", municipality="Medellín",
        name="Plaza Botero", kind=Environment.PLAZA, aliases=["parque botero"],
        lat=6.2518, lon=-75.5686,
    ))
    for ref_id, place_id, lat, lon in (
        ("ref_a", "plc_medellin_puente_4sur", 6.2089, -75.5671),
        ("ref_b", "plc_medellin_puente_4sur", 6.2087, -75.5669),
    ):
        r = _ref(ref_id, place_id, lat, lon, title=f"Foto {ref_id}")
        fpath = root / "files" / r.file_path
        make_png(fpath, 64, 48)
        r.sha256 = sha256_file(fpath)
        r.bytes = fpath.stat().st_size
        store.upsert_reference(r)
        store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
        (root / "licenses" / f"{r.ref_id}.txt").write_text("licencia de prueba\n", encoding="utf-8")
        make_png(root / "previews" / f"{r.ref_id}.jpg", 32, 24)
        store.upsert_view(View(
            view_id=f"view_{ref_id}", place_id=place_id, ref_id=ref_id,
            label="vista", lat=lat, lon=lon,
        ))
    store.upsert_coverage(Coverage(
        place_id="plc_medellin_puente_4sur", views_observed=2,
        backed={"vistas": 2}, unobserved={}, confidence_basis="2 vistas GPS",
    ))
    store.close()
    return root


@pytest.fixture()
def client(tmp_path: Path, atlas_root: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    cfg = StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=tmp_path / "data",
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-test-pass-42",
        require_auth=True,
    )
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    stub = ComfyUIStubState(healthy=True)
    transport = httpx.ASGITransport(app=create_stub_comfyui_app(stub))
    app = create_app(config=cfg, comfyui_transport=transport)
    tc = TestClient(app)
    tc.auth = ("admin", "changeme-test-pass-42")
    return tc


# ---------------------------------------------------------------------------
# API HTTP
# ---------------------------------------------------------------------------


def test_api_health_stats_places(client: TestClient) -> None:
    h = client.get("/studio/api/atlas/health")
    assert h.status_code == 200 and h.json()["available"] is True
    assert h.json()["places"] == 2
    st = client.get("/studio/api/atlas/stats")
    assert st.status_code == 200 and st.json()["references_usable"] == 2
    places = client.get("/studio/api/atlas/places", params={"q": "puente"})
    assert places.status_code == 200
    assert places.json()["count"] == 1
    assert places.json()["places"][0]["place_id"] == "plc_medellin_puente_4sur"
    mun = client.get("/studio/api/atlas/places", params={"municipality": "Envigado"})
    assert mun.json()["count"] == 0


def test_api_resolve_place_detail_views(client: TestClient) -> None:
    r = client.get("/studio/api/atlas/resolve", params={"q": "Puente de la 4 Sur"})
    assert r.json()["status"] == "confirmed"
    d = client.get("/studio/api/atlas/places/plc_medellin_puente_4sur")
    assert d.status_code == 200
    body = d.json()
    assert body["views_usable"] == 2 and body["references_usable"] == 2
    assert body["coverage"]["views_observed"] == 2
    v = client.get("/studio/api/atlas/places/plc_medellin_puente_4sur/views")
    assert v.json()["status"] == "ok" and v.json()["view"]["ref_id"].startswith("ref_")
    cov = client.get("/studio/api/atlas/places/plc_medellin_puente_4sur/coverage")
    assert cov.json()["place_id"] == "plc_medellin_puente_4sur"
    nf = client.get("/studio/api/atlas/places/plc_no_existe")
    assert nf.status_code == 404


def test_api_reference_file_preview_license(client: TestClient) -> None:
    f = client.get("/studio/api/atlas/references/ref_a/file")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png"
    assert f.headers["x-atlas-sha256"]
    p = client.get("/studio/api/atlas/references/ref_a/preview")
    assert p.status_code == 200 and p.headers["content-type"] == "image/jpeg"
    lic = client.get("/studio/api/atlas/references/ref_a/license")
    assert lic.status_code == 200 and "CC BY-SA 4.0" in lic.text
    assert client.get("/studio/api/atlas/references/ref_zzz/file").status_code == 404


def test_api_path_traversal_rechazado(client: TestClient, atlas_root: Path) -> None:
    store = AtlasStore(atlas_root / "catalog.db")
    evil = _ref("ref_evil", None, 6.2, -75.5, title="evil")
    evil.file_path = "../../../../etc/passwd"
    store.upsert_reference(evil)
    store.close()
    r = client.get("/studio/api/atlas/references/ref_evil/file")
    assert r.status_code in (400, 404)
    assert b"root:" not in r.content


def test_api_select_view_y_selection(client: TestClient) -> None:
    r = client.post("/studio/api/atlas/select-view", json={
        "place_id": "plc_medellin_puente_4sur", "lat": 6.2088, "lon": -75.5670,
        "heading": 90,
    })
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert any(lim["code"] == "heading_unknown" for lim in r.json()["limitations"])
    sel = client.get("/studio/api/atlas/selection")
    assert sel.json()["place_id"] == "plc_medellin_puente_4sur"
    assert sel.json()["view_id"].startswith("view_ref_")


def test_api_map_y_verify(client: TestClient) -> None:
    m = client.get("/studio/api/atlas/map.svg")
    assert m.status_code == 200 and m.text.startswith("<svg")
    v = client.get("/studio/api/atlas/verify", params={"quick": "true"})
    assert v.status_code == 200 and v.json()["ok"] is True


def test_api_sin_atlas_da_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "sin_atlas"
    empty.mkdir()
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(empty))
    reset_shared_atlas_service()
    cfg = StudioConfig(
        model_profile="studio",
        studio_llm_key="YOUR_STUDIO_API_KEY_HERE",
        studio_llm_fallback_key="YOUR_STUDIO_FALLBACK_KEY_HERE",
        studio_comfyui_url="http://stub-comfyui.local:39172",
        studio_data_dir=tmp_path / "data2",
        studio_stub_llm=True,
        comfy_user="admin",
        comfy_pass="changeme-test-pass-42",
        require_auth=True,
    )
    (tmp_path / "data2").mkdir(parents=True, exist_ok=True)
    stub = ComfyUIStubState(healthy=True)
    app = create_app(config=cfg, comfyui_transport=httpx.ASGITransport(
        app=create_stub_comfyui_app(stub)))
    tc = TestClient(app)
    tc.auth = ("admin", "changeme-test-pass-42")
    h = tc.get("/studio/api/atlas/health")
    assert h.status_code == 200 and h.json()["available"] is False
    assert tc.get("/studio/api/atlas/places").status_code == 503
    # el resto del estudio sigue vivo
    assert tc.get("/studio/api/health").status_code == 200


# ---------------------------------------------------------------------------
# Herramientas del agente
# ---------------------------------------------------------------------------


def test_tool_belt_incluye_atlas(atlas_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    from comfy_studio.agent.tools import create_default_tool_belt

    belt = create_default_tool_belt()
    for name in ("atlas_stats", "atlas_search_places", "atlas_resolve_place",
                 "atlas_place_detail", "atlas_select_view", "atlas_prepare_shot",
                 "atlas_verify"):
        assert belt.get(name) is not None, name


def test_atlas_prepare_shot_tool(atlas_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    shot = asyncio.run(handle_atlas_prepare_shot(
        AtlasPrepareShotArgs(place="Puente de la 4 Sur", prompt="carro sobre el puente"),
        context=None,
    ))
    assert shot["ok"] is True and shot["status"] == "ok"
    assert shot["atlas_context"]["ref_id"].startswith("ref_")
    assert Path(shot["background_abs"]).is_file()
    # hash incorrecto -> fallo tipado, no éxito falso
    make_png(atlas_root / "files" / "aa" / "ref_a.png", 64, 48, color=(1, 1, 1))
    shot2 = asyncio.run(handle_atlas_prepare_shot(
        AtlasPrepareShotArgs(place_id="plc_medellin_puente_4sur", view_id="view_ref_a"),
        context=None,
    ))
    assert shot2["status"] == "hash_mismatch" and shot2["ok"] is False


# ---------------------------------------------------------------------------
# Planner -> contexto atlas -> checkpoint; ejecutor consume el fondo real
# ---------------------------------------------------------------------------


def test_planner_inyecta_contexto_atlas_y_serialize(
    atlas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    state = ProductionState(idea="un carro sobre el puente de la 4 Sur en Medellín")
    plan_production(state, llm=StubPlannerLLM())
    assert state.metadata.get("atlas"), "el planner recibió contexto del atlas"
    assert state.metadata["atlas"]["place_id"] == "plc_medellin_puente_4sur"
    shot = state.all_shots()[0]
    gen = next(ly for ly in shot.layers if ly.kind == "gen")
    assert gen.params.get("atlas", {}).get("ref_id", "").startswith("ref_")
    # roundtrip de checkpoint conserva escenario/cámara/referencias
    clone = ProductionState.from_dict(state.to_dict())
    assert clone.metadata["atlas"]["view_id"] == state.metadata["atlas"]["view_id"]
    gen2 = next(ly for ly in clone.all_shots()[0].layers if ly.kind == "gen")
    assert gen2.params["atlas"]["ref_id"] == gen.params["atlas"]["ref_id"]


def test_executor_resuelve_fondo_atlas_real(
    atlas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    captured: list[dict] = []

    class FakeEngine:
        name = "comfyui"

        def run(self, spec):  # noqa: ANN001
            captured.append(dict(spec.job))
            from comfy_studio.engines import EngineResult

            return EngineResult(ok=True, engine="comfyui", verb="run",
                                payload={"output_path": "/tmp/out.mp4"})

    svc = shared_atlas_service()
    adapter = EngineRunnerAdapter({"comfyui": FakeEngine()}, atlas_service=svc)
    atlas_ctx = {
        "place_id": "plc_medellin_puente_4sur",
        "view_id": "view_ref_a",
        "ref_id": "ref_a",
        "background_path": "aa/ref_a.png",
    }
    res = adapter.run_layer("comfyui", {"kind": "gen", "shot": "s1", "atlas": atlas_ctx})
    assert res["ok"] is True
    job = captured[0]
    assert job["background_image"].endswith("aa/ref_a.png")
    assert Path(job["background_image"]).is_file()
    assert job["atlas_resolved"]["license"] == "CC BY-SA 4.0"
    # ref desconocida -> fallo tipado, no placeholder
    bad = adapter.run_layer("comfyui", {"kind": "gen", "atlas": {"ref_id": "ref_nope"}})
    assert bad["ok"] is False and "atlas:" in bad["error"]


def test_produccion_registrada_en_atlas(
    atlas_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_ATLAS_DIR", str(atlas_root))
    reset_shared_atlas_service()
    from comfy_studio.orchestration.service import ProductionService
    from comfy_studio.orchestration.state import ProductionState

    svc = shared_atlas_service()
    ps = ProductionService(atlas_service=svc)
    state = ProductionState(idea="plano en el puente")
    state.phase = "done"
    state.metadata["atlas"] = {
        "place_id": "plc_medellin_puente_4sur",
        "view_id": "view_ref_a",
        "ref_id": "ref_a",
        "background_path": "aa/ref_a.png",
        "camera": {"lat": 6.2089, "lon": -75.5671},
        "limitations": [],
    }
    ps._record_atlas_production(state)
    prods = svc.store.list_productions()
    assert len(prods) == 1
    assert prods[0].place_id == "plc_medellin_puente_4sur"
    assert prods[0].view_id == "view_ref_a"
    assert prods[0].params["ref_id"] == "ref_a"
    assert any(e["kind"] == "atlas_production_recorded" for e in state.log)
