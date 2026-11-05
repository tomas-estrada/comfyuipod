"""Extracto fechado de OpenStreetMap (Geofabrik) recortado al Valle de Aburrá.

- Descarga el extracto fechado `colombia-YYMMDD.osm.pbf` (fecha fijada por el
  nombre + md5 publicado + sha256 registrado), nunca "latest" mutable.
- Recorta al bbox del Valle de Aburrá con osmium (CLI si está, pyosmium si no).
- Deriva GeoJSON propios (vías con nombre, puentes, lugares) que viven en el
  atlas y sirven al mapa local y a la resolución de lugares.
- Licencia ODbL con atribución a © OpenStreetMap contributors; se conserva en
  sources y en ATTRIBUTION.md.
"""

from __future__ import annotations

import gzip
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from comfy_studio.atlas.acquire.http import HttpClient
from comfy_studio.atlas.geo import ABURRA_BBOX

GEOFABRIK_INDEX = "https://download.geofabrik.de/south-america/colombia.html"
GEOFABRIK_HOSTS = ["download.geofabrik.de"]
OSM_ATTRIBUTION = "© OpenStreetMap contributors (ODbL 1.0, https://www.openstreetmap.org/copyright)"


def discover_dated_extract(http: HttpClient) -> dict[str, str]:
    """Elige el colombia-YYMMDD.osm.pbf más reciente de la página índice."""
    html = http.request("GET", GEOFABRIK_INDEX).decode("utf-8", "replace")
    dates = re.findall(r'colombia-(\d{6})\.osm\.pbf', html)
    if not dates:
        raise RuntimeError("no se encontraron extractos fechados colombia-YYMMDD.osm.pbf")
    date = max(dates)
    base = f"https://download.geofabrik.de/south-america/colombia-{date}"
    return {
        "date": date,
        "pbf_url": f"{base}.osm.pbf",
        "md5_url": f"{base}.osm.pbf.md5",
    }


def fetch_extract(
    http: HttpClient, workdir: Path, info: dict[str, str], *, max_bytes: int = 4 * 1024**3
) -> dict[str, Any]:
    """Descarga el pbf (reanudable) y verifica el md5 publicado. Devuelve metadatos."""
    workdir.mkdir(parents=True, exist_ok=True)
    pbf = workdir / f"colombia-{info['date']}.osm.pbf"
    md5_txt = http.request("GET", info["md5_url"]).decode("ascii", "replace").strip()
    expected_md5 = md5_txt.split()[0].lower()
    http.download(info["pbf_url"], pbf, max_bytes=max_bytes, resume=True,
                  timeout_s=600.0)
    import hashlib

    h = hashlib.md5()
    with pbf.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != expected_md5:
        raise RuntimeError(f"md5 del extracto OSM no coincide: {h.hexdigest()} != {expected_md5}")
    from comfy_studio.atlas.store import sha256_file

    return {
        "path": str(pbf),
        "date": info["date"],
        "url": info["pbf_url"],
        "md5": expected_md5,
        "sha256": sha256_file(pbf),
        "bytes": pbf.stat().st_size,
    }


def clip_extract(src_pbf: Path, dst_pbf: Path, bbox: tuple[float, float, float, float]) -> None:
    """Recorta el pbf al bbox (min_lat, max_lat, min_lon, max_lon)."""
    min_lat, max_lat, min_lon, max_lon = bbox
    dst_pbf.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which("osmium"):
        subprocess.run(
            [
                "osmium", "extract",
                "--bbox", f"{min_lon},{min_lat},{max_lon},{max_lat}",
                "--overwrite", "-o", str(dst_pbf), str(src_pbf),
            ],
            check=True,
            timeout=3600,
        )
        return
    # Fallback pyosmium: recorrer con índice de ubicaciones y escribir los que caen en el bbox.
    import osmium

    dst_pbf.unlink(missing_ok=True)
    with osmium.SimpleWriter(str(dst_pbf)) as writer:
        for obj in osmium.FileProcessor(str(src_pbf)).with_locations():
            loc = getattr(obj, "location", None)
            if obj.is_node():
                if loc.valid() and min_lon <= loc.lon <= max_lon and min_lat <= loc.lat <= max_lat:
                    writer.add_node(obj)
            elif obj.is_way():
                inside = any(
                    min_lon <= n.location.lon <= max_lon and min_lat <= n.location.lat <= max_lat
                    for n in obj.nodes
                    if n.location.valid()
                )
                if inside:
                    writer.add_way(obj)
            elif obj.is_relation():
                writer.add_relation(obj)


def derive_geojson(
    pbf_path: Path, *, roads_out: Path, places_out: Path
) -> dict[str, int]:
    """Deriva GeoJSON propios desde el pbf recortado.

    roads_out: vías con nombre (LineString) + puentes marcados.
    places_out: nodos nombrados de interés (place/amenity/tourism/leisure/shop).
    """
    import osmium

    roads: list[dict[str, Any]] = []
    places: list[dict[str, Any]] = []
    min_lat, max_lat, min_lon, max_lon = ABURRA_BBOX

    class Collector(osmium.SimpleHandler):  # type: ignore[misc]
        def node(self, n: Any) -> None:
            tags = n.tags
            if not tags.get("name"):
                return
            if not n.location.valid():
                return
            if not (min_lat <= n.location.lat <= max_lat and min_lon <= n.location.lon <= max_lon):
                return
            interesting = ("place", "amenity", "tourism", "leisure", "shop", "historic")
            if any(k in tags for k in interesting):
                places.append({
                    "type": "Feature",
                    "geometry": {"type": "Point",
                                 "coordinates": [n.location.lon, n.location.lat]},
                    "properties": {
                        "osm_id": f"node/{n.id}",
                        "name": tags.get("name"),
                        "kind": next((k for k in ("place", "amenity", "tourism", "leisure")
                                      if k in tags), "other"),
                        "tags": {k: tags[k] for k in ("place", "amenity", "tourism", "leisure")
                                 if k in tags},
                    },
                })

        def way(self, w: Any) -> None:
            tags = w.tags
            name = tags.get("name") or tags.get("ref")
            if not name:
                return
            coords = [
                [n.location.lon, n.location.lat]
                for n in w.nodes
                if n.location.valid()
            ]
            if len(coords) < 2:
                return
            if not (min_lat <= min(c[1] for c in coords) <= max_lat
                    and min_lat <= max(c[1] for c in coords) <= max_lat):
                pass
            roads.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "osm_id": f"way/{w.id}",
                    "name": name,
                    "highway": tags.get("highway"),
                    "bridge": tags.get("bridge"),
                    "tunnel": tags.get("tunnel"),
                    "waterway": tags.get("waterway"),
                    "ref": tags.get("ref"),
                },
            })

    handler = Collector()
    handler.apply_file(str(pbf_path), locations=True)
    roads_out.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(roads_out, "wt", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": roads}, fh, ensure_ascii=False)
    with gzip.open(places_out, "wt", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": places}, fh, ensure_ascii=False)
    return {"roads": len(roads), "osm_places": len(places)}
