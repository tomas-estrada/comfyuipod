"""PR-4 atlas-select-view-score: el mejor fondo por score, no el primero."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.models import (
    CalibState,
    DateReliability,
    Environment,
    Place,
    View,
)
from comfy_studio.atlas.service import AtlasService
from comfy_studio.atlas.store import AtlasStore, sha256_file


def _add_sel(root: Path, store: AtlasStore, ref_id: str, *, lat: float, lon: float,
             heading: float | None = None, quality: float = 0.5,
             calib: CalibState = CalibState.UNCALIBRATED,
             capture: str | None = "2024-05-01",
             date_rel: DateReliability = DateReliability.CAPTURED) -> None:
    r = _ref(ref_id, "plc_sel", lat, lon, title=f"Vista {ref_id}")
    r.quality = quality
    r.capture_date = capture
    r.date_reliability = date_rel
    fpath = root / "files" / r.file_path
    make_png(fpath, 64, 48)
    r.sha256 = sha256_file(fpath)
    r.bytes = fpath.stat().st_size
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id=f"view_{ref_id}", place_id="plc_sel",
                           ref_id=ref_id, lat=lat, lon=lon,
                           heading_deg=heading, calib_state=calib))


@pytest.fixture()
def sel_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_sel", municipality="Medellín",
                             name="Mirador Selección", kind=Environment.MIRADOR,
                             lat=6.25, lon=-75.57))
    # duelo de criterio: mismo punto, A mejor rumbo pero peor calidad
    _add_sel(root, store, "ref_a", lat=6.25, lon=-75.57,
             heading=5.0, quality=0.5)
    _add_sel(root, store, "ref_b", lat=6.25, lon=-75.57,
             heading=30.0, quality=0.9, calib=CalibState.VERIFIED)
    # distancia: mismo resto, C a 50 m al norte
    _add_sel(root, store, "ref_c", lat=6.250452, lon=-75.57,
             heading=5.0, quality=0.5)
    store.close()
    return root


def test_calidad_y_rumbo_mejor_rumbo_medio(sel_root: Path) -> None:
    svc = AtlasService(sel_root)
    sel = svc.select_view("plc_sel", lat=6.25, lon=-75.57, heading_deg=0.0)
    assert sel["status"] == "ok"
    # A: 2*5 + 0.5*150 = 85 ; B: 2*30 + 0.1*150 - 20 = 55 → gana B
    assert sel["view"]["view_id"] == "view_ref_b"


def test_mas_cercana_gana_a_igualdad(sel_root: Path) -> None:
    svc = AtlasService(sel_root)
    sel = svc.select_view("plc_sel", lat=6.25, lon=-75.57)
    assert sel["status"] == "ok"
    assert sel["view"]["view_id"] in ("view_ref_a", "view_ref_b")
    # C está 50 m más lejos con iguales méritos que A: nunca gana a A
    assert sel["view"]["view_id"] != "view_ref_c"


def test_calibracion_verificada_desempata(sel_root: Path) -> None:
    svc = AtlasService(sel_root)
    a = svc.select_view("plc_sel", lat=6.25, lon=-75.57, heading_deg=17.0)
    # A dif 12 → 24+75=99+limitaciones ; B dif 13 → 26+15-20=21 → gana B
    assert a["view"]["view_id"] == "view_ref_b"


def test_incompatible_no_se_elige_aunque_puntue(sel_root: Path) -> None:
    svc = AtlasService(sel_root)
    sel = svc.select_view("plc_sel", lat=6.25, lon=-75.57, heading_deg=200.0)
    # A dif 155 y B dif 100 y C dif 155: todas incompatibles → unsupported
    assert sel["status"] == "unsupported_camera"
    codes = [lim["code"] for lim in sel["limitations"]]
    assert "heading_mismatch" in codes
    assert sel["available_views"], "debe listar alternativas"


def test_empate_se_rompe_por_view_id(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.upsert_place(Place(place_id="plc_sel", municipality="Medellín",
                             name="Gemelas", kind=Environment.MIRADOR,
                             lat=6.25, lon=-75.57))
    for rid in ("ref_m", "ref_n"):
        r = _ref(rid, "plc_sel", 6.25, -75.57, title=rid)
        r.quality = 0.5
        fpath = root / "files" / r.file_path
        make_png(fpath, 64, 48)
        r.sha256 = sha256_file(fpath)
        r.bytes = fpath.stat().st_size
        store.upsert_reference(r)
        store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
        store.upsert_view(View(view_id=f"view_{rid}", place_id="plc_sel",
                               ref_id=rid, lat=6.25, lon=-75.57))
    store.close()
    svc = AtlasService(root)
    try:
        sel = svc.select_view("plc_sel", lat=6.25, lon=-75.57)
        assert sel["status"] == "ok"
        assert sel["view"]["view_id"] == "view_ref_m"
    finally:
        svc.store.close()


def test_fecha_captura_pesa_mas_que_desconocida(tmp_path: Path) -> None:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.upsert_place(Place(place_id="plc_sel", municipality="Medellín",
                             name="Fechas", kind=Environment.MIRADOR,
                             lat=6.25, lon=-75.57))
    _add_sel(root, store, "ref_old", lat=6.25, lon=-75.57,
             heading=5.0, quality=0.5, capture="2024-05-01")
    _add_sel(root, store, "ref_unk", lat=6.25, lon=-75.57,
             heading=5.0, quality=0.5, capture=None,
             date_rel=DateReliability.UNKNOWN)
    store.close()
    svc = AtlasService(root)
    try:
        sel = svc.select_view("plc_sel", lat=6.25, lon=-75.57)
        assert sel["view"]["view_id"] == "view_ref_old"
    finally:
        svc.store.close()
