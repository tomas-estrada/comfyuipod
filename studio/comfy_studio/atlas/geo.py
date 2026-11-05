"""Utilidades geoespaciales stdlib del atlas.

- Indexación global en grados (WGS84) para búsqueda espacial.
- Sistemas locales métricos para reconstruir/renderizar (aprox. equirectangular
  alrededor de un origen; suficiente para sectores del Valle de Aburrá y
  registrada explícitamente como tal en las transformaciones).
- Normalización de texto para resolver alias sin confundir municipios.
"""

from __future__ import annotations

import math
import re
import unicodedata

EARTH_M_PER_DEG_LAT = 110_574.0
EARTH_M_PER_DEG_LON_BASE = 111_320.0

#: bbox del Valle de Aburrá (min_lat, max_lat, min_lon, max_lon), holgado.
ABURRA_BBOX: tuple[float, float, float, float] = (5.95, 6.55, -75.85, -75.10)


def normalize_text(value: str) -> str:
    """Minúsculas sin acentos y sin puntuación extra (para matching de nombres)."""
    nfkd = unicodedata.normalize("NFKD", value)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents.lower()).strip()


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize_text(value)).strip("-") or "x"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def m_per_deg_lon(lat: float) -> float:
    return EARTH_M_PER_DEG_LON_BASE * math.cos(math.radians(lat))


def bbox_around(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """Bbox en grados (min_lat, max_lat, min_lon, max_lon) alrededor de un punto."""
    d_lat = radius_m / EARTH_M_PER_DEG_LAT
    d_lon = radius_m / max(1.0, m_per_deg_lon(lat))
    return (lat - d_lat, lat + d_lat, lon - d_lon, lon + d_lon)


def latlon_to_xy(
    origin_lat: float, origin_lon: float, lat: float, lon: float
) -> tuple[float, float]:
    """Proyección local métrica (equirectangular) alrededor del origen.

    Convenciones registradas: eje X = este (m), eje Y = norte (m), origen en
    (origin_lat, origin_lon). Válida para sectores del tamaño del Valle de Aburrá.
    """
    x = (lon - origin_lon) * m_per_deg_lon(origin_lat)
    y = (lat - origin_lat) * EARTH_M_PER_DEG_LAT
    return x, y


def xy_to_latlon(origin_lat: float, origin_lon: float, x: float, y: float) -> tuple[float, float]:
    lon = origin_lon + x / m_per_deg_lon(origin_lat)
    lat = origin_lat + y / EARTH_M_PER_DEG_LAT
    return lat, lon


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Rumbo inicial en grados (0=N, 90=E) de (lat1,lon1) a (lat2,lon2)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def in_bbox(lat: float, lon: float, bbox: tuple[float, float, float, float]) -> bool:
    min_lat, max_lat, min_lon, max_lon = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


def bbox_intersects(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    a_min_lat, a_max_lat, a_min_lon, a_max_lon = a
    b_min_lat, b_max_lat, b_min_lon, b_max_lon = b
    return not (
        a_max_lat < b_min_lat
        or b_max_lat < a_min_lat
        or a_max_lon < b_min_lon
        or b_max_lon < a_min_lon
    )
