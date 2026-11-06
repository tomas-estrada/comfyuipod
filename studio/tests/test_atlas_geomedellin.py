"""Tests de la capa GeoMedellín (acquire) sin red ni GPU.

Cubre `license_verdict` (veredicto de licencia del metadata del servicio),
`layer_meta` y `fetch_buildings` (paginación por resultOffset, rama por
objectIds en chunks, tope de max_features y tolerancia a JSON incompleto)
con un stub de MapServer que enruta por URL/parámetros payloads realistas.
"""

from __future__ import annotations

import gzip
import json
import re
from pathlib import Path
from typing import Any

from comfy_studio.atlas.acquire.geomedellin import (
    LAYER_BUILDINGS,
    MAPSERVER,
    fetch_buildings,
    layer_meta,
    license_verdict,
)

LAYER_URL = f"{MAPSERVER}/{LAYER_BUILDINGS}"
QUERY_URL = f"{LAYER_URL}/query"
BBOX = (6.20, 6.25, -75.60, -75.50)  # min_lat, max_lat, min_lon, max_lon
_UNSET: Any = object()


def make_feature(object_id: int) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"OBJECTID": object_id, "TIPO": "HABITACIONAL"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-75.6, 6.2], [-75.59, 6.2], [-75.59, 6.21], [-75.6, 6.2]]],
        },
    }


def read_gz(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


class FakeMapServer:
    """Stub con lógica real: imita el MapServer por URL y parámetros.

    Sirve el meta pjson de la capa, objectIds y páginas geojson con el shape
    que geomedellin.py espera. `query_override` (si se pasa) sustituye la
    respuesta de `/query` para simular JSON roto o incompleto.
    """

    def __init__(
        self,
        meta: dict[str, Any],
        features: list[dict[str, Any]],
        *,
        query_override: Any = _UNSET,
    ) -> None:
        self.meta = meta
        self.features = features
        self.query_override = query_override
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        params = dict(params or {})
        self.calls.append((url, params))
        if url == LAYER_URL:
            return dict(self.meta)
        if url != QUERY_URL:
            raise AssertionError(f"URL inesperada: {url}")
        if self.query_override is not _UNSET:
            return self.query_override
        if params.get("returnIdsOnly") == "true":
            return {"objectIds": [f["properties"]["OBJECTID"] for f in self.features]}
        if "resultOffset" in params:
            start = int(params["resultOffset"])
            count = int(params["resultRecordCount"])
            return {
                "type": "FeatureCollection",
                "features": self.features[start : start + count],
            }
        where = str(params.get("where") or "")
        match = re.search(r"OBJECTID IN \(([^)]*)\)", where)
        if not match:
            raise AssertionError(f"where no soportado: {where}")
        wanted = {int(x) for x in match.group(1).split(",") if x}
        return {
            "type": "FeatureCollection",
            "features": [f for f in self.features if f["properties"]["OBJECTID"] in wanted],
        }


# --------------------------------------------------------------------------- license_verdict


def test_license_verdict_copyright_cc_by() -> None:
    verdict = license_verdict({"copyrightText": "CC BY 4.0"})
    assert verdict["redistributable"] is True
    assert verdict["license"] == "CC BY 4.0 (GeoMedellín / Alcaldía de Medellín)"
    assert verdict["evidence"] == "CC BY 4.0"
    assert "note" not in verdict


def test_license_verdict_sin_metadata() -> None:
    verdict = license_verdict({})
    assert verdict["redistributable"] is False
    assert verdict["license"] == "UNVERIFIED"
    assert verdict["evidence"] == ""
    assert "no declara licencia redistribuible" in verdict["note"]


def test_license_verdict_evidencia_en_description() -> None:
    verdict = license_verdict({"description": "Capa publicada como Open Data por la Alcaldía."})
    assert verdict["redistributable"] is True
    assert verdict["license"] == "Open Data"
    # la evidencia sólo sale de copyrightText/licenseInfo, no de description
    assert verdict["evidence"] == ""


def test_license_verdict_ambigua_y_evidencia_truncada() -> None:
    copyright_text = "Todos los derechos reservados. " * 20  # > 500 chars
    verdict = license_verdict({"copyrightText": copyright_text})
    assert verdict["redistributable"] is False
    assert verdict["license"] == "UNVERIFIED"
    assert verdict["evidence"] == copyright_text[:500]
    assert len(verdict["evidence"]) == 500
    assert "note" in verdict


def test_license_verdict_license_info_case_insensitive() -> None:
    verdict = license_verdict({"licenseInfo": "cc by 4.0"})
    assert verdict["redistributable"] is True
    assert verdict["license"] == "CC BY 4.0 (GeoMedellín / Alcaldía de Medellín)"
    assert verdict["evidence"] == "cc by 4.0"


def test_license_verdict_fronteras_de_prefijo() -> None:
    # Subcadena: "CC BY-NC" contiene el prefijo "CC BY" y hoy se acepta.
    nc = license_verdict({"copyrightText": "CC BY-NC 4.0"})
    assert nc["redistributable"] is True
    assert nc["license"] == "CC BY 4.0 (GeoMedellín / Alcaldía de Medellín)"
    # Sin tilde: el prefijo "DOMINIO PÚBLICO" no matchea "dominio publico".
    plain = license_verdict({"licenseInfo": "dominio publico"})
    assert plain["redistributable"] is False
    assert plain["license"] == "UNVERIFIED"


# --------------------------------------------------------------------------- layer_meta


def test_layer_meta_pide_pjson_de_la_capa() -> None:
    meta = {"maxRecordCount": 1000, "supportsPagination": True}
    http = FakeMapServer(meta, [])
    assert layer_meta(http) == meta
    assert http.calls == [(LAYER_URL, {"f": "pjson"})]


# --------------------------------------------------------------------------- fetch_buildings


def test_fetch_buildings_pagina_por_result_offset(tmp_path: Path) -> None:
    features = [make_feature(i) for i in range(1, 6)]
    meta = {"maxRecordCount": 2, "supportsPagination": True}
    http = FakeMapServer(meta, features)
    out = tmp_path / "capa" / "edificios.geojson.gz"
    report = fetch_buildings(http, BBOX, out)
    assert report == {"features": 5, "maxRecordCount": 2, "supportsPagination": True}
    queries = [p for u, p in http.calls if u == QUERY_URL]
    assert [p["resultOffset"] for p in queries] == ["0", "2", "4"]
    assert all(p["resultRecordCount"] == "2" for p in queries)
    assert queries[0]["geometry"] == "-75.6,6.2,-75.5,6.25"
    assert queries[0]["inSR"] == "4326"
    assert queries[0]["outSR"] == "4326"
    assert queries[0]["returnGeometry"] == "true"
    payload = read_gz(out)
    assert payload["type"] == "FeatureCollection"
    assert [f["properties"]["OBJECTID"] for f in payload["features"]] == [1, 2, 3, 4, 5]


def test_fetch_buildings_por_object_ids_en_chunks(tmp_path: Path) -> None:
    features = [make_feature(i) for i in (10, 11, 12)]
    meta = {"maxRecordCount": 2}  # sin supportsPagination → rama objectIds
    http = FakeMapServer(meta, features)
    out = tmp_path / "edificios.geojson.gz"
    report = fetch_buildings(http, BBOX, out)
    assert report == {"features": 3, "maxRecordCount": 2, "supportsPagination": False}
    queries = [p for u, p in http.calls if u == QUERY_URL]
    assert queries[0]["returnIdsOnly"] == "true"
    assert queries[0]["f"] == "json"
    assert queries[1]["where"] == "OBJECTID IN (10,11)"
    assert queries[2]["where"] == "OBJECTID IN (12)"
    payload = read_gz(out)
    assert [f["properties"]["OBJECTID"] for f in payload["features"]] == [10, 11, 12]


def test_fetch_buildings_respeta_max_features(tmp_path: Path) -> None:
    features = [make_feature(i) for i in range(1, 7)]
    meta = {"maxRecordCount": 3, "supportsPagination": True}
    http = FakeMapServer(meta, features)
    out = tmp_path / "edificios.geojson.gz"
    report = fetch_buildings(http, BBOX, out, max_features=4)
    assert report["features"] == 4
    payload = read_gz(out)
    assert [f["properties"]["OBJECTID"] for f in payload["features"]] == [1, 2, 3, 4]


def test_fetch_buildings_tolerates_json_incompleto(tmp_path: Path) -> None:
    # Meta vacía → defaults (1000, sin paginación) y /query sin cuerpo.
    http = FakeMapServer({}, [], query_override=None)
    out = tmp_path / "edificios.geojson.gz"
    report = fetch_buildings(http, BBOX, out)
    assert report == {"features": 0, "maxRecordCount": 1000, "supportsPagination": False}
    assert read_gz(out) == {"type": "FeatureCollection", "features": []}
    # Meta con clave nula y respuestas sin objectIds también se toleran.
    http2 = FakeMapServer({"maxRecordCount": None}, [make_feature(1)], query_override={})
    report2 = fetch_buildings(http2, BBOX, tmp_path / "b.geojson.gz")
    assert report2 == {"features": 0, "maxRecordCount": 1000, "supportsPagination": False}
