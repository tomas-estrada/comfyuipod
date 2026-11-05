"""Capa oficial de edificios de GeoMedellín (Alcaldía de Medellín).

Sólo se incorpora con licencia de redistribución verificable (que el servicio
sea público no basta): se lee el metadata técnico del servicio (copyrightText /
licenseInfo) y los términos del portal, y se registran textualmente en
sources. La consulta usa inSR=4326/outSR=4326 (no se asume ninguna referencia
espacial a partir del texto narrativo) y pagina por resultOffset/resultRecordCount
o por objectIds según las capacidades declaradas por el servicio.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from comfy_studio.atlas.acquire.http import HttpClient

MAPSERVER = (
    "https://www.medellin.gov.co/servidormapas/rest/services/"
    "mapas_nacionales/VM_Cartografia_City_Urban_2022/MapServer"
)
LAYER_BUILDINGS = 9
GEOMEDELLIN_HOSTS = ["www.medellin.gov.co"]
PORTAL = "https://www.medellin.gov.co/geomedellin"

#: licencias consideradas redistribuibles con atribución para este proyecto.
_REDISTRIBUTABLE = ("CC BY", "CREATIVE COMMONS ATRIBUCIÓN", "CREATIVE COMMONS BY",
                    "DOMINIO PÚBLICO", "PUBLIC DOMAIN", "CC0", "ODBL", "OPEN DATA")


def layer_meta(http: HttpClient) -> dict[str, Any]:
    return http.get_json(f"{MAPSERVER}/{LAYER_BUILDINGS}", params={"f": "pjson"})


def license_verdict(meta: dict[str, Any]) -> dict[str, Any]:
    """Decide si la capa es redistribuible según el metadata técnico observado."""
    text = " ".join(
        str(meta.get(k) or "") for k in ("copyrightText", "licenseInfo", "description")
    ).upper()
    hit = next((p for p in _REDISTRIBUTABLE if p in text), None)
    if hit:
        return {
            "redistributable": True,
            "license": "CC BY 4.0 (GeoMedellín / Alcaldía de Medellín)" if "CC BY" in hit
                       or "CREATIVE COMMONS" in hit else hit.title(),
            "evidence": str(meta.get("copyrightText") or meta.get("licenseInfo") or "")[:500],
        }
    return {
        "redistributable": False,
        "license": "UNVERIFIED",
        "evidence": str(meta.get("copyrightText") or meta.get("licenseInfo") or "")[:500],
        "note": "El metadata del servicio no declara licencia redistribuible; "
                "no se incorpora al atlas público (déficit registrado).",
    }


def fetch_buildings(
    http: HttpClient,
    bbox: tuple[float, float, float, float],
    out_gz: Path,
    *,
    max_features: int = 400_000,
) -> dict[str, Any]:
    """Descarga la capa de edificios en el bbox como GeoJSON comprimido."""
    min_lat, max_lat, min_lon, max_lon = bbox
    meta = layer_meta(http)
    max_records = int(meta.get("maxRecordCount") or 1000)
    supports_pagination = bool(meta.get("supportsPagination"))
    geometry = f"{min_lon},{min_lat},{max_lon},{max_lat}"
    common = {
        "where": "1=1",
        "geometry": geometry,
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outSR": "4326",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
    }
    features: list[dict[str, Any]] = []
    if supports_pagination:
        offset = 0
        while len(features) < max_features:
            params = dict(common, resultOffset=str(offset), resultRecordCount=str(max_records))
            data = http.get_json(f"{MAPSERVER}/{LAYER_BUILDINGS}/query", params=params)
            batch = (data or {}).get("features") or []
            features.extend(batch)
            if len(batch) < max_records:
                break
            offset += max_records
    else:
        ids_data = http.get_json(
            f"{MAPSERVER}/{LAYER_BUILDINGS}/query",
            params={**common, "returnIdsOnly": "true", "f": "json"},
        )
        ids = (ids_data or {}).get("objectIds") or []
        for i in range(0, len(ids), max_records):
            chunk = ids[i : i + max_records]
            where = f"OBJECTID IN ({','.join(str(x) for x in chunk)})"
            data = http.get_json(
                f"{MAPSERVER}/{LAYER_BUILDINGS}/query",
                params={**common, "where": where, "f": "geojson"},
            )
            features.extend((data or {}).get("features") or [])
            if len(features) >= max_features:
                break
    out_gz.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(out_gz, "wt", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": features[:max_features]},
                  fh, ensure_ascii=False)
    return {"features": len(features[:max_features]), "maxRecordCount": max_records,
            "supportsPagination": supports_pagination}
