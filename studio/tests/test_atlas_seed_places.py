"""Invariantes de datos puros de `acquire/seeds.py` (sin GPU, sin red).

Ancla el estado real de las constantes de semilla: estructura de las tuplas,
cubrimiento de municipios, validez de las bbox y pertenencia de cada seed a la
bbox de su municipio (con la excepción documentada de Parque Arví).
"""

from __future__ import annotations

from comfy_studio.atlas.acquire.seeds import (
    MUNICIPALITY_BBOX,
    SEED_CATEGORIES,
    SEED_PLACES,
)

#: límites del Valle de Aburrá (lat 5.95–6.55, lon -75.85..-75.1).
ABURRA_BBOX = (5.95, 6.55, -75.85, -75.1)


def test_seed_places_nonempty_and_entry_count() -> None:
    assert len(SEED_PLACES) == 56


def test_seed_places_structure_minimal_fields() -> None:
    for entry in SEED_PLACES:
        name, municipality, kind, aliases, lat, lon, radius = entry
        assert name and municipality and kind
        assert isinstance(aliases, tuple) and aliases
        assert all(isinstance(a, str) and a for a in aliases)
        assert isinstance(lat, float) and isinstance(lon, float)
        assert radius > 0


def test_municipality_bbox_keys_covered_by_seeds() -> None:
    seed_municipalities = {entry[1] for entry in SEED_PLACES}
    # cada municipio con bbox al menos tiene una seed, y viceversa.
    assert set(MUNICIPALITY_BBOX) <= seed_municipalities
    assert seed_municipalities <= set(MUNICIPALITY_BBOX)


def test_municipality_bbox_ordered() -> None:
    for municipality, (min_lat, max_lat, min_lon, max_lon) in MUNICIPALITY_BBOX.items():
        assert min_lat < max_lat, municipality
        assert min_lon < max_lon, municipality


def test_seed_coordinates_within_aburra_bbox() -> None:
    min_lat, max_lat, min_lon, max_lon = ABURRA_BBOX
    for name, _municipality, _kind, _aliases, lat, lon, _radius in SEED_PLACES:
        assert min_lat <= lat <= max_lat, name
        assert min_lon <= lon <= max_lon, name


def test_seed_within_own_municipality_bbox_with_arvi_exception() -> None:
    # Excepción legítima: "Parque Arví mirador" (6.318, -75.498) cae fuera de
    # la bbox de Medellín (max_lon -75.50) porque el parque se extiende más
    # allá del perímetro municipal; sigue dentro de ABURRA_BBOX. Es un dato
    # real, no un error: se ancla aquí para no "corregir" los datos.
    arvi = next(e for e in SEED_PLACES if e[0] == "Parque Arví mirador")
    min_lat, max_lat, min_lon, max_lon = MUNICIPALITY_BBOX[arvi[1]]
    assert not (min_lat <= arvi[4] <= max_lat and min_lon <= arvi[5] <= max_lon)
    assert arvi[5] > max_lon  # fuera solo por el extremo oriental

    for entry in SEED_PLACES:
        name, municipality, _kind, _aliases, lat, lon, _radius = entry
        if name == "Parque Arví mirador":
            continue
        min_lat, max_lat, min_lon, max_lon = MUNICIPALITY_BBOX[municipality]
        assert min_lat <= lat <= max_lat, name
        assert min_lon <= lon <= max_lon, name


def test_seed_names_unique() -> None:
    names = [entry[0] for entry in SEED_PLACES]
    assert len(names) == len(set(names))


def test_seed_coordinates_unique() -> None:
    coords = [(entry[4], entry[5]) for entry in SEED_PLACES]
    assert len(coords) == len(set(coords))


def test_seed_kinds_vocabulary() -> None:
    # kinds no son únicos por diseño (hay 13 "park", 14 "road_corridor", ...);
    # se ancla el vocabulario cerrado que hoy usan las semillas.
    kinds = {entry[2] for entry in SEED_PLACES}
    assert kinds == {
        "bridge",
        "road_corridor",
        "street",
        "park",
        "plaza",
        "mirador",
        "river",
        "hub",
        "intersection",
    }


def test_seed_categories_nonempty_unique() -> None:
    assert len(SEED_CATEGORIES) == 12
    assert all(isinstance(c, str) and c for c in SEED_CATEGORIES)
    assert len(set(SEED_CATEGORIES)) == len(SEED_CATEGORIES)


def test_seed_aliases_globally_unique() -> None:
    aliases = [a for entry in SEED_PLACES for a in entry[3]]
    assert len(aliases) == len(set(aliases))
