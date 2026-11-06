"""Mantenimiento de acquire/pipeline.py: fix/build/mark/ensure/refine.

Estado vacío y estado sembrado con la evidencia exacta que cada rama necesita.
Store sqlite real en tmp_path (CPU, sin red); ningún test invoca
run_osm/run_geomedellin/run_acquisition ni descarga nada.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_atlas_core import _ref

from comfy_studio.atlas.acquire.pipeline import MUNICIPALITY_UNKNOWN, AcquisitionPipeline
from comfy_studio.atlas.geo import haversine_m
from comfy_studio.atlas.models import (
    Environment,
    Place,
    ScenarioKind,
    ScenarioStatus,
    View,
)


@pytest.fixture()
def mant_pipe(tmp_path: Path) -> AcquisitionPipeline:
    """Pipeline con raíz en tmp_path: store sqlite real, sin red."""
    return AcquisitionPipeline(tmp_path / "atlas", max_refs=10, dry_run=False, min_interval_s=0.0)


# ---------------------------------------------------------------------------
# fix_place_municipalities
# ---------------------------------------------------------------------------


def test_fix_place_municipalities_vacio(mant_pipe: AcquisitionPipeline) -> None:
    assert mant_pipe.fix_place_municipalities() == {"fixed": 0}


def test_fix_place_municipalities_corrige_y_conserva(mant_pipe: AcquisitionPipeline) -> None:
    store = mant_pipe.svc.store
    # 6.25/-75.57 cae únicamente en el bbox de Medellín entre los municipios del Valle
    store.upsert_place(Place(
        place_id="plc_mal", municipality="Bello", name="Lote Mal Etiquetado",
        kind=Environment.OTHER, lat=6.25, lon=-75.57,
    ))
    store.upsert_place(Place(
        place_id="plc_ok", municipality="Medellín", name="Lote Correcto",
        kind=Environment.OTHER, lat=6.25, lon=-75.57,
    ))
    # sin coordenadas no hay bbox que aplicar: se conserva lo que traiga
    store.upsert_place(Place(
        place_id="plc_sin", municipality="Medellín", name="Sin Coordenadas",
        kind=Environment.OTHER,
    ))
    # fuera de todo bbox del Valle: nunca se fuerza a Medellín, queda "desconocido"
    store.upsert_place(Place(
        place_id="plc_fuera", municipality="Medellín", name="Fuera del Valle",
        kind=Environment.OTHER, lat=5.0, lon=-74.0,
    ))

    assert mant_pipe.fix_place_municipalities() == {"fixed": 2}

    places = {p.place_id: p for p in store.list_places(limit=1000)}
    assert places["plc_mal"].municipality == "Medellín"
    assert places["plc_ok"].municipality == "Medellín"
    assert places["plc_sin"].municipality == "Medellín"
    assert places["plc_fuera"].municipality == MUNICIPALITY_UNKNOWN


# ---------------------------------------------------------------------------
# build_scenarios
# ---------------------------------------------------------------------------


def test_build_scenarios_vacio(mant_pipe: AcquisitionPipeline) -> None:
    assert mant_pipe.build_scenarios() == {"photo_bg": 0, "multiview": 0}


def test_build_scenarios_genera_photo_bg_y_multiview(mant_pipe: AcquisitionPipeline) -> None:
    store = mant_pipe.svc.store
    # una sola vista fondo_urbano alcanza para photo_bg (verificado)
    store.upsert_place(Place(
        place_id="plc_fondo", municipality="Medellín", name="Fondo Simple",
        kind=Environment.STREET, lat=6.25, lon=-75.57,
    ))
    store.upsert_reference(_ref("ref_f1", "plc_fondo", 6.2501, -75.5701, title="Fachada norte"))
    store.upsert_view(View(
        view_id="view_f1", place_id="plc_fondo", ref_id="ref_f1", lat=6.2501, lon=-75.5701,
    ))

    # 5 vistas GPS en <=200 m: además candidata multiview (borrador, nunca validada)
    store.upsert_place(Place(
        place_id="plc_mv", municipality="Medellín", name="Cluster Multiview",
        kind=Environment.PLAZA, lat=6.25, lon=-75.57,
    ))
    mv_ids = []
    for i in range(5):
        rid = f"ref_mv{i}"
        lat = 6.2500 + i * 0.0002
        lon = -75.5700 + i * 0.0002
        store.upsert_reference(_ref(rid, "plc_mv", lat, lon, title=f"Vista {i} del cluster"))
        store.upsert_view(View(
            view_id=f"view_mv{i}", place_id="plc_mv", ref_id=rid, lat=lat, lon=lon,
        ))
        mv_ids.append(rid)

    counts = mant_pipe.build_scenarios()
    assert counts == {"photo_bg": 2, "multiview": 1}

    by_id = {s.scenario_id: s for s in store.all_scenarios()}
    assert set(by_id) == {"scn_plc_fondo_v1", "scn_plc_mv_v1", "scn_plc_mv_mv_v1"}

    fondo = by_id["scn_plc_fondo_v1"]
    assert fondo.kind == ScenarioKind.PHOTO_BG
    assert fondo.status == ScenarioStatus.VERIFIED
    assert fondo.refs == ["ref_f1"]
    assert fondo.capabilities["preserve_background_pixels"] is True
    assert fondo.capabilities["free_camera_translation"] is False

    mv_photo = by_id["scn_plc_mv_v1"]
    assert mv_photo.kind == ScenarioKind.PHOTO_BG
    assert mv_photo.status == ScenarioStatus.VERIFIED
    assert set(mv_photo.refs) == set(mv_ids)

    mv = by_id["scn_plc_mv_mv_v1"]
    assert mv.kind == ScenarioKind.MULTIVIEW
    assert mv.status == ScenarioStatus.DRAFT
    assert mv.capabilities["colmap_ready"] is True
    assert mv.capabilities["reconstruction_validated"] is False
    assert "5 vistas" in mv.capabilities["overlap_evidence"]


def test_build_scenarios_negativos_sin_fondo_o_lejos(mant_pipe: AcquisitionPipeline) -> None:
    store = mant_pipe.svc.store
    # usable pero NO fondo_urbano: no genera escenario
    store.upsert_place(Place(
        place_id="plc_nofondo", municipality="Medellín", name="Sin Fondo",
        kind=Environment.STREET, lat=6.25, lon=-75.57,
    ))
    ref_nf = _ref("ref_nf", "plc_nofondo", 6.2501, -75.5701, title="Primer plano")
    ref_nf.content_class = "retrato_persona"
    store.upsert_reference(ref_nf)
    store.upsert_view(View(
        view_id="view_nf", place_id="plc_nofondo", ref_id="ref_nf", lat=6.2501, lon=-75.5701,
    ))

    # 5 vistas fondo_urbano pero con spread > 200 m: photo_bg sí, multiview no
    store.upsert_place(Place(
        place_id="plc_lejos", municipality="Medellín", name="Vistas Lejanas",
        kind=Environment.PLAZA, lat=6.25, lon=-75.57,
    ))
    for i in range(5):
        rid = f"ref_lj{i}"
        lat = 6.2500 + i * 0.0010
        lon = -75.5800 + i * 0.0002
        store.upsert_reference(_ref(rid, "plc_lejos", lat, lon, title=f"Placa {i}"))
        store.upsert_view(View(
            view_id=f"view_lj{i}", place_id="plc_lejos", ref_id=rid, lat=lat, lon=lon,
        ))

    assert mant_pipe.build_scenarios() == {"photo_bg": 1, "multiview": 0}
    assert [s.scenario_id for s in store.all_scenarios()] == ["scn_plc_lejos_v1"]


# ---------------------------------------------------------------------------
# mark_non_background_refs
# ---------------------------------------------------------------------------


def test_mark_non_background_refs_vacio(mant_pipe: AcquisitionPipeline) -> None:
    assert mant_pipe.mark_non_background_refs() == 0


def test_mark_non_background_refs_marca_primer_plano(mant_pipe: AcquisitionPipeline) -> None:
    store = mant_pipe.svc.store
    store.upsert_place(Place(
        place_id="plc_m", municipality="Medellín", name="Lote M",
        kind=Environment.STREET, lat=6.25, lon=-75.57,
    ))
    flor = _ref("ref_flor", "plc_m", 6.2501, -75.5701, title="Flor de Valle")
    limpio = _ref("ref_limpio", "plc_m", 6.2502, -75.5702, title="Puente de la 80")
    ya = _ref("ref_ya", "plc_m", 6.2503, -75.5703, title="Flor marchita")
    ya.usable = False  # ya descartada: sale del barrido usable_only=True
    for r in (flor, limpio, ya):
        store.upsert_reference(r)

    assert mant_pipe.mark_non_background_refs() == 1

    after = {r.ref_id: r for r in store.all_references()}
    assert after["ref_flor"].usable is False
    assert after["ref_flor"].reject_reason == "primer plano / no usable como fondo de plano"
    assert after["ref_limpio"].usable is True
    assert after["ref_limpio"].reject_reason is None
    assert after["ref_ya"].usable is False
    assert after["ref_ya"].reject_reason is None


# ---------------------------------------------------------------------------
# ensure_municipality_sector_places
# ---------------------------------------------------------------------------


def _seed_cluster_refs(store, start: int, count: int) -> None:
    """Referencias usables GPS en la misma celda de rejilla (~1 km)."""
    for i in range(start, start + count):
        store.upsert_reference(_ref(
            f"ref_sector{i}", None,
            6.2510 + i * 0.0006, -75.5740 + i * 0.0001,
            title=f"Vista de sector {i}",
        ))


def test_ensure_municipality_sector_places_vacio(mant_pipe: AcquisitionPipeline) -> None:
    assert mant_pipe.ensure_municipality_sector_places() == {"sectors_created": 0}


def test_ensure_municipality_sector_places_crea_desde_cluster(
    mant_pipe: AcquisitionPipeline,
) -> None:
    store = mant_pipe.svc.store
    _seed_cluster_refs(store, 0, 3)
    # por debajo de min_refs=5 no inventa geometría
    assert mant_pipe.ensure_municipality_sector_places() == {"sectors_created": 0}

    _seed_cluster_refs(store, 3, 2)
    assert mant_pipe.ensure_municipality_sector_places() == {"sectors_created": 1}

    sectors = [p for p in store.list_places(limit=1000) if p.sector == "cluster_referencias"]
    assert len(sectors) == 1
    sector = sectors[0]

    lats = [6.2510 + i * 0.0006 for i in range(5)]
    lons = [-75.5740 + i * 0.0001 for i in range(5)]
    clat, clon = sum(lats) / 5, sum(lons) / 5
    max_d = max(haversine_m(clat, clon, la, lo) for la, lo in zip(lats, lons, strict=True))

    assert sector.place_id == "plc_medellin_sector_00"
    assert sector.name == "Medellín (sector urbano)"
    assert sector.municipality == "Medellín"
    assert sector.kind == Environment.STREET
    assert sector.lat == pytest.approx(clat)
    assert sector.lon == pytest.approx(clon)
    assert sector.provenance["source"] == "commons_gps_cluster"
    assert sector.provenance["method"] == "centroid of geotagged references"
    assert sector.provenance["refs"] == 5
    assert sector.provenance["radius_m"] == round(max_d)
    assert sector.bounds is not None
    assert sector.bounds[0] < clat < sector.bounds[1]
    assert sector.bounds[2] < clon < sector.bounds[3]


def test_ensure_municipality_sector_places_omite_si_ya_hay_vistas(
    mant_pipe: AcquisitionPipeline,
) -> None:
    store = mant_pipe.svc.store
    store.upsert_place(Place(
        place_id="plc_pre", municipality="Medellín", name="Parque Preexistente",
        kind=Environment.PARK, lat=6.2500, lon=-75.5700,
    ))
    store.upsert_reference(_ref("ref_pre", "plc_pre", 6.2500, -75.5700, title="Parque existente"))
    store.upsert_view(View(
        view_id="view_pre", place_id="plc_pre", ref_id="ref_pre", lat=6.2500, lon=-75.5700,
    ))
    _seed_cluster_refs(store, 0, 5)

    # el municipio ya tiene lugar con vistas: el clúster no aporta sector nuevo
    assert mant_pipe.ensure_municipality_sector_places() == {"sectors_created": 0}
    assert not any(p.sector == "cluster_referencias" for p in store.list_places(limit=1000))


# ---------------------------------------------------------------------------
# refine_seed_coordinates
# ---------------------------------------------------------------------------


def test_refine_seed_coordinates_vacio(mant_pipe: AcquisitionPipeline) -> None:
    assert mant_pipe.refine_seed_coordinates() == {"seeds_refined": 0}


def test_refine_seed_coordinates_refina_solo_con_evidencia(mant_pipe: AcquisitionPipeline) -> None:
    store = mant_pipe.svc.store
    seed_meta = {"source": "seed", "method": "approx_coordinate"}

    # A: 2 coincidencias cerca -> se refina al centroide GPS
    store.upsert_place(Place(
        place_id="plc_ok", municipality="Medellín", name="Parque Pruebas",
        kind=Environment.PARK, lat=6.2510, lon=-75.5720, provenance=dict(seed_meta),
    ))
    store.upsert_reference(_ref(
        "ref_ok1", None, 6.2514, -75.5723, title="Parque Pruebas vista norte",
    ))
    store.upsert_reference(_ref(
        "ref_ok2", None, 6.2516, -75.5717, title="Parque Pruebas vista sur",
    ))

    # B: solo 1 coincidencia (< min_refs=2) -> no se toca
    store.upsert_place(Place(
        place_id="plc_poco", municipality="Medellín", name="Mirador Pruebas",
        kind=Environment.MIRADOR, lat=6.2550, lon=-75.5750, provenance=dict(seed_meta),
    ))
    store.upsert_reference(_ref("ref_poco", None, 6.2551, -75.5751, title="Mirador Pruebas unico"))

    # C: 2 coincidencias pero el centroide supera max_shift_m (2000 m) -> no se toca
    store.upsert_place(Place(
        place_id="plc_lejos", municipality="Medellín", name="Lago Pruebas",
        kind=Environment.RIVER, lat=6.2500, lon=-75.5700, provenance=dict(seed_meta),
    ))
    store.upsert_reference(_ref("ref_lejo1", None, 6.2900, -75.5700, title="Lago Pruebas norte"))
    store.upsert_reference(_ref("ref_lejo2", None, 6.2910, -75.5700, title="Lago Pruebas sur"))

    # D: sin provenance seed -> fuera del barrido
    store.upsert_place(Place(
        place_id="plc_noseed", municipality="Medellín", name="Barrio Pruebas",
        kind=Environment.STREET, lat=6.2560, lon=-75.5760,
    ))
    store.upsert_reference(_ref("ref_ns1", None, 6.2561, -75.5761, title="Barrio Pruebas norte"))
    store.upsert_reference(_ref("ref_ns2", None, 6.2563, -75.5759, title="Barrio Pruebas sur"))

    assert mant_pipe.refine_seed_coordinates() == {"seeds_refined": 1}

    ok = store.get_place("plc_ok")
    assert ok is not None
    assert ok.lat == pytest.approx(6.2515)
    assert ok.lon == pytest.approx(-75.5720)
    assert ok.provenance["source"] == "seed+commons_gps_cluster"
    assert ok.provenance["method"] == "centroid of matching geotagged references"
    assert ok.provenance["refs"] == 2

    poco = store.get_place("plc_poco")
    assert poco is not None
    assert poco.lat == pytest.approx(6.2550)
    assert poco.provenance["source"] == "seed"

    lejos = store.get_place("plc_lejos")
    assert lejos is not None
    assert lejos.lat == pytest.approx(6.2500)
    assert lejos.provenance["source"] == "seed"

    noseed = store.get_place("plc_noseed")
    assert noseed is not None
    assert noseed.provenance == {}
