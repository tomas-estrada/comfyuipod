"""PR-1 atlas-search-rank: FTS5 acentos+prefijo, ranking y paginación honesta."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.geo import normalize_text
from comfy_studio.atlas.models import Environment, Place
from comfy_studio.atlas.service import AtlasService
from comfy_studio.atlas.store import AtlasStore


@pytest.fixture()
def search_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    assert store.get_meta("schema_version") == "4"
    store.set_meta("atlas_version", "1.0.0-test")
    places = [
        Place(place_id="plc_botero", municipality="Medellín", name="Plaza Botero",
              kind=Environment.PLAZA, aliases=["parque botero"],
              lat=6.2518, lon=-75.5686),
        Place(place_id="plc_sur", municipality="Medellín", name="Puente de la 4 Sur",
              kind=Environment.BRIDGE, aliases=["puente 4 sur"],
              lat=6.2088, lon=-75.5670),
        Place(place_id="plc_env", municipality="Envigado", name="Parque de Envigado",
              kind=Environment.PARK, aliases=[],
              lat=6.1699, lon=-75.5920),
    ]
    for p in places:
        store.upsert_place(p)
    for ref_id, pid, lat, lon, title in [
        ("ref_botero", "plc_botero", 6.2519, -75.5687, "Plaza Botero"),
        ("ref_sur", "plc_sur", 6.2089, -75.5671, "Puente 4 Sur vista norte"),
        ("ref_env", "plc_env", 6.1700, -75.5921, "Parque Envigado"),
    ]:
        r = _ref(ref_id, pid, lat, lon, title=title)
        fpath = root / "files" / r.file_path
        # _ref usa aa/<ref_id>.png por defecto; ajustar al ref_id real
        from comfy_studio.atlas.store import sha256_file

        make_png(fpath, 32, 32)
        r.sha256 = sha256_file(fpath)
        r.bytes = fpath.stat().st_size
        store.upsert_reference(r)
        store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
        from comfy_studio.atlas.models import View

        store.upsert_view(View(view_id=f"view_{ref_id}", place_id=pid,
                               ref_id=ref_id, lat=lat, lon=lon))
    store.close()
    return root


def test_acento_insensible(search_root: Path) -> None:
    svc = AtlasService(search_root)
    res = svc.search_places(q="medellin plaza botero")
    assert any(p["place_id"] == "plc_botero" for p in res)
    # con tilde también
    res2 = svc.search_places(q="Medellín Plaza Botero")
    assert any(p["place_id"] == "plc_botero" for p in res2)
    # token suelto sin tilde casa con municipio con tilde
    res3 = svc.search_places(q="medellin")
    assert {p["place_id"] for p in res3} >= {"plc_botero", "plc_sur"}


def test_prefijo_fts(search_root: Path) -> None:
    svc = AtlasService(search_root)
    res = svc.search_places(q="boter")
    assert any(p["place_id"] == "plc_botero" for p in res)
    res = svc.search_places(q="puente 4 s")
    assert any(p["place_id"] == "plc_sur" for p in res)


def test_alias_exacto_prioriza(search_root: Path) -> None:
    svc = AtlasService(search_root)
    res = svc.search_places(q="parque botero")
    assert res and res[0]["place_id"] == "plc_botero"
    confirmed = svc.resolve_place("parque botero")
    assert confirmed["status"] == "confirmed"
    assert confirmed["place"]["place_id"] == "plc_botero"


def test_municipio_y_bbox_filtran_con_q(search_root: Path) -> None:
    svc = AtlasService(search_root)
    res = svc.search_places(q="parque", municipality="Envigado")
    assert {p["place_id"] for p in res} == {"plc_env"}
    # bbox que solo cubre Envigado (6.16,-75.59) excluye Medellín aunque haya match textual
    bbox = (6.15, 6.18, -75.61, -75.58)
    res = svc.search_places(q="parque", bbox=bbox)
    assert {p["place_id"] for p in res} == {"plc_env"}


def test_paginacion_honesta(search_root: Path) -> None:
    svc = AtlasService(search_root)
    all_res = svc.search_places(q="medellin", limit=10, offset=0)
    assert len(all_res) >= 2
    p1 = svc.search_places(q="medellin", limit=1, offset=0)
    p2 = svc.search_places(q="medellin", limit=1, offset=1)
    assert len(p1) == 1 and len(p2) == 1
    assert p1[0]["place_id"] != p2[0]["place_id"]
    assert {p1[0]["place_id"], p2[0]["place_id"]} <= {p["place_id"] for p in all_res}


def test_normalize_sin_acentos() -> None:
    assert normalize_text("Medellín") == "medellin"
    assert normalize_text("Puente de la 4 Sur") == "puente de la 4 sur"
