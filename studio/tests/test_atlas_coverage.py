"""PR-2 atlas-coverage-honesta: cobertura medida en vivo (vistas, rumbos, licencias)."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_atlas_core import _ref, make_png

from comfy_studio.atlas.models import Coverage, Environment, Place, View
from comfy_studio.atlas.service import AtlasService, heading_quadrant
from comfy_studio.atlas.store import AtlasStore, sha256_file


def _add_ref(root: Path, store: AtlasStore, ref_id: str, place_id: str,
             lat: float, lon: float, *, heading: float | None = None,
             capture_date: str | None = "2024-05-01",
             license: str = "CC BY-SA 4.0", usable: bool = True) -> None:
    r = _ref(ref_id, place_id, lat, lon, title=f"Foto {ref_id}")
    r.capture_date = capture_date
    r.license = license
    r.usable = usable
    fpath = root / "files" / r.file_path
    make_png(fpath, 32, 32)
    r.sha256 = sha256_file(fpath)
    r.bytes = fpath.stat().st_size
    store.upsert_reference(r)
    store.set_license(r.ref_id, r.license, r.license_url, r.attribution, None)
    store.upsert_view(View(view_id=f"view_{ref_id}", place_id=place_id,
                           ref_id=ref_id, lat=lat, lon=lon,
                           heading_deg=heading, usable=usable))


@pytest.fixture()
def cov_root(tmp_path: Path) -> Path:
    root = tmp_path / "atlas"
    (root / "files" / "aa").mkdir(parents=True)
    (root / "previews").mkdir(parents=True)
    (root / "licenses").mkdir(parents=True)
    store = AtlasStore(root / "catalog.db")
    store.set_meta("atlas_version", "1.0.0-test")
    store.upsert_place(Place(place_id="plc_norte_sur", municipality="Medellín",
                             name="Mirador Norte Sur", kind=Environment.MIRADOR,
                             lat=6.25, lon=-75.57))
    store.upsert_place(Place(place_id="plc_vacio", municipality="Bello",
                             name="Lote Vacío", kind=Environment.PLAZA,
                             lat=6.33, lon=-75.55))
    store.upsert_place(Place(place_id="plc_mixto", municipality="Envigado",
                             name="Parque Mixto", kind=Environment.PARK,
                             lat=6.17, lon=-75.59))
    _add_ref(root, store, "ref_n", "plc_norte_sur", 6.2501, -75.57,
             heading=10.0, capture_date="2024-01-15")
    _add_ref(root, store, "ref_s", "plc_norte_sur", 6.2499, -75.57,
             heading=190.0, capture_date="2024-05-01")
    _add_ref(root, store, "ref_u", "plc_mixto", 6.17, -75.59,
             heading=None, capture_date="2023-06-01", license="CC0-1.0")
    _add_ref(root, store, "ref_x", "plc_mixto", 6.17, -75.59,
             heading=90.0, capture_date="2023-07-01", usable=False)
    store.close()
    return root


def test_quadrantes() -> None:
    assert heading_quadrant(10.0) == "N"
    assert heading_quadrant(90.0) == "E"
    assert heading_quadrant(190.0) == "S"
    assert heading_quadrant(270.0) == "W"
    assert heading_quadrant(350.0) == "N"


def test_cubre_norte_sur_falta_este_oeste(cov_root: Path) -> None:
    svc = AtlasService(cov_root)
    cov = svc.coverage_for("plc_norte_sur")
    assert cov.views_observed == 2
    assert cov.backed["quadrants_covered"] == ["N", "S"]
    assert cov.unobserved["quadrants_missing"] == ["E", "W"]
    assert cov.backed["headings_deg"] == [10.0, 190.0]
    assert cov.unobserved["heading_unknown"] == 0


def test_fechas_y_licencias_reales(cov_root: Path) -> None:
    svc = AtlasService(cov_root)
    cov = svc.coverage_for("plc_norte_sur")
    assert cov.evidence_oldest == "2024-01-15"
    assert cov.evidence_newest == "2024-05-01"
    assert cov.backed["licenses"] == {"CC BY-SA 4.0": 2}
    assert cov.confidence_basis == "2 vistas usables, 2 rumbos conocidos, 1 licencias"


def test_sin_vistas_es_honesto(cov_root: Path) -> None:
    svc = AtlasService(cov_root)
    cov = svc.coverage_for("plc_vacio")
    assert cov.views_observed == 0
    assert cov.backed["quadrants_covered"] == []
    assert cov.unobserved["quadrants_missing"] == ["N", "E", "S", "W"]
    assert cov.evidence_newest is None and cov.evidence_oldest is None
    detail = svc.place_detail("plc_vacio")
    assert detail["views_usable"] == 0
    assert detail["coverage"]["views_observed"] == 0


def test_excluye_vistas_no_usables_y_rumbo_desconocido(cov_root: Path) -> None:
    svc = AtlasService(cov_root)
    cov = svc.coverage_for("plc_mixto")
    # ref_x no usable (vista y referencia) queda fuera; ref_u sin rumbo cuenta
    assert cov.views_observed == 1
    assert cov.backed["quadrants_covered"] == []
    assert cov.unobserved == {"quadrants_missing": ["N", "E", "S", "W"],
                              "heading_unknown": 1}
    assert cov.backed["licenses"] == {"CC0-1.0": 1}


def test_recompute_repara_valor_guardado_obsoleto(cov_root: Path) -> None:
    svc = AtlasService(cov_root)
    svc.store.upsert_coverage(Coverage(
        place_id="plc_norte_sur", views_observed=9,
        backed={"vistas": 9}, unobserved={},
        evidence_newest="2030-01-01", evidence_oldest="2030-01-01",
        confidence_basis="9 vistas inventadas",
    ))
    # la lectura en vivo ya es honesta pese al valor guardado
    assert svc.place_detail("plc_norte_sur")["coverage"]["views_observed"] == 2
    assert svc.search_places(q="mirador")[0]["coverage"]["views_observed"] == 2
    fixed = svc.recompute_coverage("plc_norte_sur")
    assert fixed.views_observed == 2
    assert fixed.evidence_newest == "2024-05-01"
    stored = svc.store.get_coverage("plc_norte_sur")
    assert stored is not None and stored.views_observed == 2
    assert stored.backed["quadrants_covered"] == ["N", "S"]
