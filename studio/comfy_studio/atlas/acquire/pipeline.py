"""Pipeline de adquisición del atlas (reanudable, con caché por contenido).

Orquesta las fuentes autorizadas y pobla la raíz del atlas:

    osm         extracto Geofabrik fechado -> clip al Aburrá -> GeoJSON propios
    geomedellin capa de edificios (solo si licencia redistribuible verificable)
    commons     categorías semilla + geosearch por lugar + búsqueda por municipio

Selección por contenido real: licencia permitida, MIME raster, tamaño/dimensiones,
deduplicación por sha1 del original y por phash, asignación al lugar más cercano
con GPS dentro del radio, rechazo de mapas/diagramas/interiores obvios por título.
Cada archivo descargado se verifica (hash, MIME, dimensiones) antes de promoverse;
cada referencia guarda autoría, licencia, fecha de captura vs subida, revisión
consultada, procedencia y hashes. Un 429/403 suspende la fuente (circuit breaker),
nunca destruye el inventario.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from comfy_studio.atlas.acquire import DEFAULT_USER_AGENT
from comfy_studio.atlas.acquire.commons import (
    COMMONS_HOSTS,
    CommonsClient,
    license_verdict,
    page_url,
)
from comfy_studio.atlas.acquire.geofabrik import (
    GEOFABRIK_HOSTS,
    OSM_ATTRIBUTION,
    clip_extract,
    derive_geojson,
    discover_dated_extract,
    fetch_extract,
)
from comfy_studio.atlas.acquire.geomedellin import (
    GEOMEDELLIN_HOSTS,
    LAYER_BUILDINGS,
    MAPSERVER,
    PORTAL,
    fetch_buildings,
    layer_meta,
)
from comfy_studio.atlas.acquire.geomedellin import (
    license_verdict as geomedellin_license_verdict,
)
from comfy_studio.atlas.acquire.http import CircuitOpenError, HttpClient, HttpError
from comfy_studio.atlas.acquire.seeds import MUNICIPALITY_BBOX, SEED_CATEGORIES, SEED_PLACES
from comfy_studio.atlas.geo import ABURRA_BBOX, haversine_m, in_bbox, normalize_text
from comfy_studio.atlas.imaging import image_dimensions, is_raster_mime, sniff_mime
from comfy_studio.atlas.models import (
    DateReliability,
    Environment,
    Place,
    Reference,
    ResolutionClass,
    SourceRecord,
    View,
)
from comfy_studio.atlas.service import ATLAS_VERSION, AtlasService
from comfy_studio.atlas.store import sha256_file

#: hosts permitidos para adquisición (allowlist; nada más sale del proceso).
ALLOWED_HOSTS = COMMONS_HOSTS + GEOFABRIK_HOSTS + GEOMEDELLIN_HOSTS

#: techo por archivo original descargado; si el original excede, se guarda la
#: miniatura oficial a THUMB_WIDTH (registrada como derived_downscale).
MAX_ORIGINAL_BYTES = 4 * 1024 * 1024
THUMB_WIDTH = 2560
MIN_WIDTH = 1280
MAX_REF_BYTES = 8 * 1024 * 1024

_REJECT_TITLE = re.compile(
    r"(mapa|map|plan\b|plano|diagram|diagrama|logo|escudo|bandera|flag|retrato|portrait|"
    r"selfie|interior|maqueta|render 3d|dibujo|drawing|painting|pintura|"
    r"grabado|engraving|poster|afiche|moneda|billete|sello|stamp|"
    r"abeja|insecto|mariposa|araña|spider|avispa|hormiga|"
    r"flor|flores|planta|árbol|arbol|hoja|macro|primer plano|"
    r"perro|gato|mascota|animal|ave|pájaro|pajaro|"
    r"persona|hombre|mujer|niño|nino|cara|rostro|dedo|mano|mano)",
    re.IGNORECASE,
)
_HISTORICAL_YEAR = 2005


def _dummy_ref():
    from comfy_studio.atlas.models import Reference

    return Reference(ref_id="", title="", file_path="", sha256="", source="")


def _env_from_kind(kind: str) -> Environment:
    try:
        return Environment(kind)
    except ValueError:
        return Environment.OTHER


def _phash(path: Path) -> str | None:
    """phash perceptual (8x8 average hash) si Pillow está disponible; None si no."""
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("L").resize((9, 8))
            px = list(im.getdata())
            avg = sum(px) / len(px)
            bits = 0
            for i, v in enumerate(px[:64]):
                bits |= (1 if v > avg else 0) << i
            return f"{bits:016x}"
    except Exception:  # noqa: BLE001
        return None


class AcquisitionPipeline:
    """Ejecuta la adquisición por fuentes y escribe en la raíz del atlas."""

    def __init__(
        self,
        root: Path,
        *,
        max_refs: int = 650,
        dry_run: bool = False,
        user_agent: str = DEFAULT_USER_AGENT,
        min_interval_s: float = 0.4,
    ) -> None:
        self.root = Path(root)
        self.max_refs = max_refs
        self.dry_run = dry_run
        self.svc = AtlasService(self.root, create=True)
        self.http = HttpClient(
            user_agent,
            ALLOWED_HOSTS,
            min_interval_s=min_interval_s,
            timeout_s=60.0,
            circuit_failures=6,
            circuit_cooldown_s=120.0,
        )
        self.commons = CommonsClient(self.http, thumb_width=THUMB_WIDTH)
        self.run_id = "main"  # estable: permite reanudar entre ejecuciones

    # ------------------------------------------------------------------ fuentes

    def run_osm(self) -> dict[str, Any]:
        store = self.svc.store
        self.svc.store.begin_run(self.run_id, "osm")
        try:
            info = discover_dated_extract(self.http)
            workdir = self.root / "osm" / ".cache"
            meta = fetch_extract(self.http, workdir, info)
            dst = self.root / "osm" / "aburra-valley.osm.pbf"
            clip_extract(Path(meta["path"]), dst, ABURRA_BBOX)
            counts = derive_geojson(
                dst,
                roads_out=self.root / "osm" / "roads.geojson.gz",
                places_out=self.root / "osm" / "places_osm.geojson.gz",
            )
            src = SourceRecord(
                source_id="osm_geofabrik_colombia",
                name=(
                    f"OpenStreetMap extracto Colombia {info['date']} (Geofabrik) "
                    "recortado al Valle de Aburrá"
                ),
                kind="osm_extract",
                url=info["pbf_url"],
                license="ODbL 1.0",
                license_url="https://www.openstreetmap.org/copyright",
                terms=OSM_ATTRIBUTION,
                extract_date=info["date"],
                sha256=sha256_file(dst),
                notes=f"original sha256={meta['sha256']} md5={meta['md5']} bytes={meta['bytes']}; "
                      f"clip bbox={ABURRA_BBOX}",
            )
            store.upsert_source(src)
            store.set_meta("osm_extract_date", info["date"])
            store.set_meta("osm_clip_sha256", sha256_file(dst))
            self._derive_places_from_osm()
            store.end_run(self.run_id, "osm", "done", {**counts, "extract": info["date"]})
            return {"ok": True, "extract_date": info["date"], **counts}
        except Exception as exc:  # noqa: BLE001
            self.svc.store.end_run(self.run_id, "osm", "failed", {}, error=str(exc)[:500])
            return {"ok": False, "error": str(exc)[:500]}

    def _derive_places_from_osm(self) -> int:
        """Crea lugares desde entidades OSM nombradas (en una sola transacción)."""
        store = self.svc.store
        with store.batch():
            return self._derive_places_from_osm_inner()

    def _derive_places_from_osm_inner(self) -> int:
        """Crea lugares desde entidades OSM nombradas (puentes, miradores, parques)."""
        import gzip as _gzip

        store = self.svc.store
        places_path = self.root / "osm" / "places_osm.geojson.gz"
        roads_path = self.root / "osm" / "roads.geojson.gz"
        created = 0
        existing_norm = {normalize_text(p.name) for p in store.list_places(limit=100000)}
        if places_path.is_file():
            with _gzip.open(places_path, "rt", encoding="utf-8") as fh:
                gj = json.load(fh)
            for feat in gj.get("features", []):
                props = feat.get("properties", {})
                name = props.get("name") or ""
                if not name or normalize_text(name) in existing_norm:
                    continue
                kind = "other"
                tags = props.get("tags", {})
                if tags.get("tourism") == "viewpoint":
                    kind = "mirador"
                elif tags.get("leisure") == "park":
                    kind = "park"
                elif tags.get("place") in ("square", "plaza"):
                    kind = "plaza"
                elif tags.get("amenity") in ("townhall", "library", "theatre", "museum"):
                    kind = "hub"
                lon, lat = feat["geometry"]["coordinates"]
                municipality = _municipality_for(lat, lon)
                import hashlib as _hl
                pid = "plc_" + normalize_text(name).replace(" ", "_")[:30] + "_" + _hl.sha1(
                    (props.get("osm_id") or name).encode()).hexdigest()[:8]
                store.upsert_place(Place(
                    place_id=pid,
                    municipality=municipality,
                    name=name,
                    kind=_env_from_kind(kind),
                    lat=lat, lon=lon,
                    osm_refs=[props.get("osm_id", "")],
                    provenance={
                        "source": "osm_places_geojson",
                        "osm_id": props.get("osm_id"),
                        "municipality_method": "bbox_heuristic_no_verificado",
                    },
                ))
                existing_norm.add(normalize_text(name))
                created += 1
        if roads_path.is_file():
            with _gzip.open(roads_path, "rt", encoding="utf-8") as fh:
                gj = json.load(fh)
            for feat in gj.get("features", []):
                props = feat.get("properties", {})
                if not props.get("bridge") or props["bridge"] in ("no",):
                    continue
                name = props.get("name") or ""
                if not name or normalize_text(name) in existing_norm:
                    continue
                coords = feat["geometry"]["coordinates"]
                lat = sum(c[1] for c in coords) / len(coords)
                lon = sum(c[0] for c in coords) / len(coords)
                import hashlib as _hl2
                pid = "plc_" + normalize_text(name).replace(" ", "_")[:30] + "_" + _hl2.sha1(
                    (props.get("osm_id") or name).encode()).hexdigest()[:8]
                store.upsert_place(Place(
                    place_id=pid,
                    municipality=_municipality_for(lat, lon),
                    name=name,
                    kind=Environment.BRIDGE,
                    lat=lat, lon=lon,
                    osm_refs=[props.get("osm_id", "")],
                    provenance={
                        "source": "osm_roads_bridge",
                        "osm_id": props.get("osm_id"),
                        "municipality_method": "bbox_heuristic_no_verificado",
                    },
                ))
                existing_norm.add(normalize_text(name))
                created += 1
        return created

    def run_geomedellin(self) -> dict[str, Any]:
        store = self.svc.store
        store.begin_run(self.run_id, "geomedellin")
        try:
            meta = layer_meta(self.http)
            verdict = geomedellin_license_verdict(meta)
            src = SourceRecord(
                source_id="geomedellin_buildings_2022",
                name="GeoMedellín VM_Cartografia_City_Urban_2022 capa 9 (edificios con altura AGL)",
                kind="wms_layer",
                url=f"{MAPSERVER}/{LAYER_BUILDINGS}",
                license=verdict["license"] if verdict["redistributable"] else "UNVERIFIED",
                license_url=PORTAL if verdict["redistributable"] else None,
                terms=verdict.get("evidence", "")[:500],
                extract_date=None,
                notes=("capa consultada con inSR=4326/outSR=4326; "
                       + ("redistribuible según metadata." if verdict["redistributable"]
                          else "NO redistribuible según metadata: no se publica.")),
            )
            if not verdict["redistributable"]:
                store.upsert_source(src)
                store.end_run(self.run_id, "geomedellin", "skipped_license_unverified",
                              {"evidence": verdict.get("evidence", "")[:300]})
                return {"ok": False, "skipped": "license_unverified",
                        "evidence": verdict.get("evidence", "")[:300]}
            out = self.root / "geodata" / "buildings-aburra.geojson.gz"
            res = fetch_buildings(self.http, ABURRA_BBOX, out)
            from comfy_studio.atlas.store import sha256_file as _sha

            src.sha256 = _sha(out)
            store.upsert_source(src)
            store.end_run(self.run_id, "geomedellin", "done", res)
            return {"ok": True, **res}
        except Exception as exc:  # noqa: BLE001
            store.end_run(self.run_id, "geomedellin", "failed", {}, error=str(exc)[:500])
            return {"ok": False, "error": str(exc)[:500]}

    # ------------------------------------------------------------------ lugares

    def ensure_seed_places(self) -> int:
        store = self.svc.store
        created = 0
        with store.batch():
            created = self._ensure_seed_places_inner(store)
        return created

    def _ensure_seed_places_inner(self, store) -> int:  # type: ignore[no-untyped-def]
        created = 0
        for name, municipality, kind, aliases, lat, lon, radius in SEED_PLACES:
            norm = normalize_text(name)
            existing = store.get_place_by_norm_name(norm, municipality)
            if existing:
                ex = existing[0]
                changed = False
                if ex.lat != lat or ex.lon != lon or sorted(ex.aliases) != sorted(aliases):
                    ex.lat, ex.lon = lat, lon
                    ex.aliases = list(aliases)
                    ex.provenance = {**(ex.provenance or {}), "source": "seed",
                                     "assign_radius_m": radius, "refined": True}
                    store.upsert_place(ex)
                    changed = True
                if changed:
                    created += 1
                continue
            mun_slug = normalize_text(municipality).replace(" ", "-")[:12]
            store.upsert_place(Place(
                place_id=f"plc_{mun_slug}_{norm.replace(' ', '_')[:36]}",
                municipality=municipality,
                name=name,
                kind=_env_from_kind(kind),
                aliases=list(aliases),
                lat=lat, lon=lon,
                provenance={"source": "seed", "method": "approx_coordinate",
                            "assign_radius_m": radius},
            ))
            created += 1
        return created

    # ------------------------------------------------------------------ commons

    def _collect_candidates(self) -> list[dict[str, Any]]:
        """Candidatos (título + gps) de categorías semilla + geosearch por lugar.

        Se cachean en disco (.cache/candidates.json) para que las reanudaciones
        no repitan las consultas a la API.
        """
        cache_path = self.root / ".cache" / "candidates.json"
        if cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if data:
                    print(f"  candidatos desde caché: {len(data)}", flush=True)
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        store = self.svc.store
        candidates: dict[str, dict[str, Any]] = {}

        def add(title: str, lat: float | None, lon: float | None) -> None:
            if not title:
                return
            if title not in candidates:
                candidates[title] = {"title": title, "lat": lat, "lon": lon}
            elif lat is not None and candidates[title]["lat"] is None:
                # fusionar coordenadas de geosearch con el título ya visto en categorías
                candidates[title]["lat"] = lat
                candidates[title]["lon"] = lon

        for cat in SEED_CATEGORIES:
            if len(candidates) >= 4000:
                break
            try:
                for title in self.commons.category_files(
                    cat, max_depth=1, max_items=400, max_subcats=6, max_pages=4
                ):
                    add(title, None, None)
            except (HttpError, CircuitOpenError) as exc:
                store.acq_item(self.run_id, "commons", f"cat:{cat}", "suspended", str(exc)[:200])
            print(f"  cat {cat}: candidatos={len(candidates)}", flush=True)
        # geosearch alrededor de cada lugar semilla con coordenadas
        places = store.list_places(limit=100000)
        geo_targets = [
            p for p in places
            if p.lat is not None and p.lon is not None
            and (
                (p.provenance or {}).get("source") == "seed"
                or str(p.kind) in ("bridge", "mirador", "park", "plaza")
            )
        ][:250]
        for i, p in enumerate(geo_targets):
            if len(candidates) >= 8000:
                break
            radius = int((p.provenance or {}).get("assign_radius_m", 1500))
            if (p.provenance or {}).get("source") != "seed":
                radius = 1500
            try:
                for hit in self.commons.geosearch(p.lat, p.lon, min(radius, 3500), limit=150):
                    add(hit["title"], hit.get("lat"), hit.get("lon"))
            except (HttpError, CircuitOpenError):
                pass
            if i % 25 == 0:
                print(f"  geosearch {i}/{len(geo_targets)}: candidatos={len(candidates)}",
                      flush=True)
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cache_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(list(candidates.values()), ensure_ascii=False),
                                   encoding="utf-8")
                    tmp.replace(cache_path)
                except OSError:
                    pass
        # barrido amplio por centro de cada municipio
        for municipality, (min_lat, max_lat, min_lon, max_lon) in MUNICIPALITY_BBOX.items():
            if len(candidates) >= 8000:
                break
            clat = (min_lat + max_lat) / 2
            clon = (min_lon + max_lon) / 2
            try:
                for hit in self.commons.geosearch(clat, clon, 6000, limit=500):
                    add(hit["title"], hit.get("lat"), hit.get("lon"))
            except (HttpError, CircuitOpenError):
                pass
            print(f"  geosearch municipio {municipality}: candidatos={len(candidates)}",
                  flush=True)
        # búsqueda textual por municipio para ampliar cobertura
        for municipality in MUNICIPALITY_BBOX:
            try:
                for title in self.commons.search_files(
                    f"{municipality} Medellín exterior", limit=100
                ):
                    add(title, None, None)
            except (HttpError, CircuitOpenError) as exc:
                store.acq_item(self.run_id, "commons", f"search:{municipality}", "suspended",
                               str(exc)[:200])
        out = list(candidates.values())
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return out

    def run_commons(self) -> dict[str, Any]:
        store = self.svc.store
        store.begin_run(self.run_id, "commons")
        stats = {"candidates": 0, "rejected": 0, "downloaded": 0, "skipped_resume": 0,
                 "suspended": 0, "errors": []}
        try:
            candidates = self._collect_candidates()
            stats["candidates"] = len(candidates)
            # imageinfo en lotes (cacheado en disco para reanudar rápido)
            titles = [c["title"] for c in candidates]
            infos_cache = self.root / ".cache" / "infos.json"
            if infos_cache.is_file():
                try:
                    infos = json.loads(infos_cache.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    infos = {}
            else:
                infos = {}
            missing = [t for t in titles if t not in infos]
            if missing:
                print(f"  imageinfo de {len(missing)} títulos nuevos...", flush=True)
                infos.update(self.commons.imageinfo(missing))
                try:
                    infos_cache.parent.mkdir(parents=True, exist_ok=True)
                    tmp = infos_cache.with_suffix(".tmp")
                    tmp.write_text(json.dumps(infos, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(infos_cache)
                except OSError:
                    pass
            print(f"  imageinfo ok: {len(infos)}", flush=True)
            # índice de lugares para asignación
            places = store.list_places(limit=100000)
            place_pts = [
                (p, p.lat, p.lon) for p in places
                if p.lat is not None and p.lon is not None
            ]

            selected: list[dict[str, Any]] = []
            seen_sha1: set[str] = set()
            # primero los candidatos con coordenadas (geosearch): son los que
            # producen vistas utilizables; luego el resto (categorías)
            ordered = sorted(candidates, key=lambda c: (c.get("lat") is None, c["title"]))
            for c in ordered:
                info = infos.get(c["title"])
                if not info:
                    stats["rejected"] += 1
                    continue
                ok, lic = license_verdict(info.get("license_short"))
                if not ok:
                    stats["rejected"] += 1
                    store.acq_item(self.run_id, "commons", c["title"], "rejected_license",
                                   info.get("license_short", ""))
                    continue
                mime = info.get("mime") or ""
                if not is_raster_mime(mime) or mime == "image/svg+xml":
                    stats["rejected"] += 1
                    continue
                width = int(info.get("width") or 0)
                if width and width < MIN_WIDTH:
                    stats["rejected"] += 1
                    continue
                title_l = (c["title"] + " " + (info.get("description") or "")).lower()
                if _REJECT_TITLE.search(title_l):
                    stats["rejected"] += 1
                    store.acq_item(self.run_id, "commons", c["title"], "rejected_content",
                                   "mapa/diagrama/retrato/interior")
                    continue
                sha1 = (info.get("sha1") or "").lower()
                if sha1 and sha1 in seen_sha1:
                    stats["rejected"] += 1
                    continue
                if sha1:
                    seen_sha1.add(sha1)
                lat = c.get("lat") or info.get("gps_lat")
                lon = c.get("lon") or info.get("gps_lon")
                if lat is not None and lon is not None and not in_bbox(lat, lon, ABURRA_BBOX):
                    stats["rejected"] += 1
                    store.acq_item(self.run_id, "commons", c["title"], "rejected_geo",
                                   "GPS fuera del Valle de Aburrá")
                    continue
                selected.append({**info, "lat": lat, "lon": lon})
                if len(selected) >= self.max_refs * 3:
                    break

            # asignar a lugar más cercano y preparar descargas (fetch paralelo)
            to_fetch: list[dict[str, Any]] = []
            for info in selected:
                if len(to_fetch) >= self.max_refs * 2:
                    break
                title = info["title"]
                if store.acq_item_status(self.run_id, "commons", title) == "done":
                    stats["skipped_resume"] += 1
                    continue
                place_id = None
                if info.get("lat") is not None and info.get("lon") is not None:
                    best = None
                    for pl, plat, plon in place_pts:
                        d = haversine_m(info["lat"], info["lon"], plat, plon)
                        radius = 500
                        if (pl.provenance or {}).get("source") == "seed":
                            radius = int((pl.provenance or {}).get("assign_radius_m", 500))
                        else:
                            radius = 400
                        if d <= radius and (best is None or d < best[1]):
                            best = (pl, d)
                    if best:
                        place_id = best[0].place_id
                _, lic = license_verdict(info.get("license_short"))
                plan = self._plan_fetch(info, place_id, lic)
                if plan is not None:
                    to_fetch.append(plan)
            print(f"  planes de descarga: {len(to_fetch)} (ya hechos: {stats['skipped_resume']})",
                  flush=True)

            from concurrent.futures import ThreadPoolExecutor

            def _fetch_one(plan: dict[str, Any]) -> tuple[dict[str, Any], Any, str | None]:
                try:
                    res = self.http.download(
                        plan["download_url"],
                        plan["dest"],
                        # el sha1 de Commons es del ORIGINAL: sólo verificable
                        # cuando descargamos el original, no la miniatura
                        sha1=None if plan["use_thumb"] else plan["sha1_source"],
                        max_bytes=MAX_REF_BYTES,
                        resume=True,
                        timeout_s=300.0,
                    )
                    return plan, res, None
                except (HttpError, CircuitOpenError) as exc:
                    return plan, None, f"{type(exc).__name__}: {exc}"[:200]
                except Exception as exc:  # noqa: BLE001
                    return plan, None, str(exc)[:200]

            stop = False
            with ThreadPoolExecutor(max_workers=6) as pool:
                for plan, res, err in pool.map(_fetch_one, to_fetch):
                    if stop:
                        continue
                    if stats["downloaded"] + stats["skipped_resume"] >= self.max_refs:
                        stop = True
                        continue
                    key = plan["title"]
                    if err is not None:
                        if "CircuitOpen" in err:
                            stats["suspended"] += 1
                            store.acq_item(self.run_id, "commons", key, "suspended", err)
                            stats["errors"].append(
                                "circuit abierto en commons; se detiene la fuente"
                            )
                            stop = True
                            continue
                        if "Forbidden" in err or " 403" in err:
                            stats["suspended"] += 1
                            store.acq_item(self.run_id, "commons", key, "suspended", err)
                            continue
                        store.acq_item(self.run_id, "commons", key, "failed", err)
                        stats["errors"].append(f"{key}: {err}")
                        continue
                    ref = self._build_reference(plan, res)
                    if ref is None:
                        stats["rejected"] += 1
                        store.acq_item(self.run_id, "commons", key, "rejected_content",
                                       "no decodificable")
                        continue
                    store.upsert_reference(ref)
                    store.set_license(ref.ref_id, ref.license, ref.license_url,
                                      ref.attribution, None)
                    self._write_license_file(ref)
                    if ref.gps_lat is not None and ref.gps_lon is not None and plan["place_id"]:
                        store.upsert_view(View(
                            view_id=f"view_{ref.ref_id}",
                            place_id=plan["place_id"],
                            ref_id=ref.ref_id,
                            label=ref.title[:120],
                            lat=ref.gps_lat,
                            lon=ref.gps_lon,
                            calib_state="uncalibrated",
                            method="gps_metadata",
                        ))
                    store.acq_item(self.run_id, "commons", key, "done",
                                   f"{ref.ref_id} place={plan['place_id']}")
                    stats["downloaded"] += 1
                    if stats["downloaded"] % 25 == 0:
                        print(f"  refs listas {stats['downloaded']}/{self.max_refs}", flush=True)
            self._dedupe_phash()
            # Auditar antes de medir: solo `fondo_urbano` cuenta como cobertura
            # usable para fondos de plano (la medición honesta lo exige).
            self.purge_library()
            self.svc.write_manifest()
            self._write_attribution()
            store.end_run(self.run_id, "commons", "done", stats)
            stats["ok"] = True
            return stats
        except Exception as exc:  # noqa: BLE001
            store.end_run(self.run_id, "commons", "failed", stats, error=str(exc)[:500])
            return {"ok": False, "error": str(exc)[:500], **stats}

    def _plan_fetch(
        self, info: dict[str, Any], place_id: str | None, license_canon: str
    ) -> dict[str, Any] | None:
        """Decide URL/destino para una referencia (original si cabe, thumb si no)."""
        if self.dry_run:
            return None
        title = info["title"]
        size = int(info.get("size") or 0)
        url = info.get("url")
        thumb = info.get("thumburl")
        use_thumb = size > MAX_ORIGINAL_BYTES and bool(thumb)
        if not use_thumb and size > MAX_REF_BYTES:
            return None
        download_url = thumb if use_thumb else url
        if not download_url:
            return None
        ext = ".jpg" if info.get("mime") == "image/jpeg" else (
            ".png" if info.get("mime") == "image/png" else ".img"
        )
        ref_id = (
            "ref_" + re.sub(r"[^a-z0-9]+", "_", normalize_text(title))[:40]
            + "_" + (info.get("sha1") or title)[:8].lower()
        )
        rel = f"{ref_id[:2]}/{ref_id}{ext}"
        return {
            "info": info,
            "title": title,
            "place_id": place_id,
            "license_canon": license_canon,
            "ref_id": ref_id,
            "rel": rel,
            "dest": self.root / "files" / rel,
            "download_url": download_url,
            "use_thumb": use_thumb,
            "sha1_source": (info.get("sha1") or "").lower() or None,
        }

    def _build_reference(self, plan: dict[str, Any], res: dict[str, Any]) -> Reference | None:
        """Verifica firma/dimensiones del archivo descargado y construye la Reference."""
        info = plan["info"]
        dest = plan["dest"]
        mime = sniff_mime(dest)
        if mime is None or not is_raster_mime(mime):
            dest.unlink(missing_ok=True)
            return None
        dims = image_dimensions(dest)
        if dims is None:
            dest.unlink(missing_ok=True)
            return None
        w, h = dims
        capture = info.get("capture_date") or ""
        m = re.search(r"(\d{4})", capture)
        if m and int(m.group(1)) < _HISTORICAL_YEAR:
            date_rel = DateReliability.HISTORICAL
        elif capture:
            date_rel = DateReliability.CAPTURED
        elif info.get("timestamp"):
            date_rel = DateReliability.UPLOADED_ONLY
        else:
            date_rel = DateReliability.UNKNOWN
        ref = Reference(
            ref_id=plan["ref_id"],
            title=plan["title"].replace("File:", ""),
            file_path=plan["rel"],
            sha256=res["sha256"],
            source="wikimedia_commons",
            place_id=plan["place_id"],
            source_page=page_url(plan["title"]),
            source_file_url=plan["download_url"],
            sha1_source=plan["sha1_source"],
            author=info.get("artist") or None,
            license=plan["license_canon"],
            license_url=info.get("license_url") or None,
            attribution=(
                f"{info.get('artist') or 'Autor desconocido'} — "
                f"{plan['license_canon']} — {page_url(plan['title'])}"
            ),
            capture_date=capture or None,
            upload_date=info.get("timestamp"),
            date_reliability=date_rel,
            width=w,
            height=h,
            bytes=res["bytes"],
            mime=mime,
            resolution_class=(
                ResolutionClass.DERIVED_DOWNSCALE if plan["use_thumb"]
                else ResolutionClass.ORIGINAL
            ),
            derived_from_url=info.get("url") if plan["use_thumb"] else None,
            gps_lat=info.get("lat"),
            gps_lon=info.get("lon"),
            description=info.get("description") or None,
            quality=_quality_score(info, w, h),
            usable=True,
        )
        ph = _phash(dest)
        if ph:
            ref.phash = ph
        self._make_preview(ref.ref_id, dest)
        return ref

    def _make_preview(self, ref_id: str, src: Path) -> None:
        try:
            from PIL import Image  # type: ignore
        except Exception:  # noqa: BLE001
            return
        try:
            out = self.root / "previews" / f"{ref_id}.jpg"
            out.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((512, 512))
                im.save(out, "JPEG", quality=80)
        except Exception:  # noqa: BLE001
            return

    def _write_license_file(self, ref: Reference) -> None:
        lic_dir = self.root / "licenses"
        lic_dir.mkdir(parents=True, exist_ok=True)
        text = (
            f"Referencia: {ref.title}\n"
            f"Fuente: {ref.source_page}\n"
            f"Archivo: {ref.source_file_url}\n"
            f"Autor: {ref.author or 'desconocido'}\n"
            f"Licencia: {ref.license} ({ref.license_url or 'sin URL'})\n"
            f"Fecha de captura: {ref.capture_date or 'desconocida'} "
            f"(fiabilidad: {ref.date_reliability})\n"
            f"Fecha de subida: {ref.upload_date or 'desconocida'}\n"
            f"sha256 local: {ref.sha256}\n"
            f"sha1 original Commons: {ref.sha1_source or 'n/d'}\n"
            f"Resolución: {ref.width}x{ref.height} ({ref.resolution_class})\n"
            f"Atribución: {ref.attribution}\n"
        )
        (lic_dir / f"{ref.ref_id}.txt").write_text(text, encoding="utf-8")

    @staticmethod
    def _view_capture_date(store: Any, view: View) -> str | None:
        ref = store.get_reference(view.ref_id)
        return ref.capture_date if ref else None

    def refine_seed_coordinates(
        self, *, min_refs: int = 2, max_shift_m: int = 2000
    ) -> dict[str, int]:
        """Refina la coordenada de cada semilla con el centroide GPS de las
        referencias cuyo título coincide con el nombre/alias (evidencia real,
        registrada en provenance)."""
        store = self.svc.store
        refs = [r for r in store.all_references(usable_only=True)
                if r.gps_lat is not None and r.gps_lon is not None]
        seeds = [p for p in store.list_places(limit=100000)
                 if (p.provenance or {}).get("source") == "seed"]
        moved = 0
        with store.batch():
            for seed in seeds:
                if seed.lat is None or seed.lon is None:
                    continue
                tokens = set(normalize_text(seed.name).split())
                for a in seed.aliases:
                    tokens.update(normalize_text(a).split())
                tokens = {t for t in tokens if len(t) >= 4}
                if not tokens:
                    continue
                hits = [
                    r for r in refs
                    if tokens and all(t in normalize_text(r.title) for t in list(tokens)[:3])
                ]
                if len(hits) < min_refs:
                    continue
                lat = sum(r.gps_lat for r in hits) / len(hits)
                lon = sum(r.gps_lon for r in hits) / len(hits)
                if haversine_m(seed.lat, seed.lon, lat, lon) > max_shift_m:
                    continue
                seed.lat, seed.lon = lat, lon
                seed.provenance = {
                    **seed.provenance,
                    "source": "seed+commons_gps_cluster",
                    "method": "centroid of matching geotagged references",
                    "refs": len(hits),
                }
                store.upsert_place(seed)
                moved += 1
        return {"seeds_refined": moved}

    def fix_place_municipalities(self) -> dict[str, int]:
        """Reetiqueta el municipio de cada lugar con la regla de bbox específica."""
        store = self.svc.store
        fixed = 0
        with store.batch():
            for p in store.list_places(limit=100000):
                if p.lat is None or p.lon is None:
                    continue
                mun = _municipality_for(p.lat, p.lon)
                if mun != p.municipality:
                    p.municipality = mun
                    store.upsert_place(p)
                    fixed += 1
        return {"fixed": fixed}

    def ensure_municipality_sector_places(self, *, min_refs: int = 5) -> dict[str, int]:
        """Crea lugares-sector desde clústeres de referencias GPS por municipio.

        Para municipios del Valle con pocas entidades OSM/semilla, el clúster de
        fotos georreferenciadas define un sector real (centroide + radio), con
        procedencia explícita. No inventa geometría: es la envolvente de las
        referencias.
        """
        store = self.svc.store
        from comfy_studio.atlas.acquire.seeds import MUNICIPALITY_BBOX

        refs = [r for r in store.all_references(usable_only=True)
                if r.gps_lat is not None and r.gps_lon is not None]
        by_mun: dict[str, list] = {}
        for r in refs:
            mun = _municipality_for(r.gps_lat, r.gps_lon)
            by_mun.setdefault(mun, []).append(r)
        created = 0
        with store.batch():
            for mun in MUNICIPALITY_BBOX:
                mun_refs = by_mun.get(mun, [])
                # si el municipio ya tiene lugares con vistas, no hace falta sector
                with_views = [p for p in store.list_places(municipality=mun, limit=500)
                              if store.views_for_place(p.place_id)]
                if with_views or len(mun_refs) < min_refs:
                    continue
                # clúster simple por rejilla de ~600 m
                cell: dict[tuple[int, int], list] = {}
                for r in mun_refs:
                    cell.setdefault((int(r.gps_lat * 100), int(r.gps_lon * 100)), []).append(r)
                best_cell = max(cell.values(), key=len)
                if len(best_cell) < min_refs:
                    continue
                lat = sum(r.gps_lat for r in best_cell) / len(best_cell)
                lon = sum(r.gps_lon for r in best_cell) / len(best_cell)
                max_d = max(haversine_m(lat, lon, r.gps_lat, r.gps_lon) for r in best_cell)
                pid = f"plc_{normalize_text(mun)[:12]}_sector_{created:02d}"
                store.upsert_place(Place(
                    place_id=pid,
                    municipality=mun,
                    name=f"{mun} (sector urbano)",
                    kind=Environment.STREET,
                    sector="cluster_referencias",
                    lat=lat,
                    lon=lon,
                    bounds=(lat - max_d / 110574, lat + max_d / 110574,
                            lon - max_d / 111320, lon + max_d / 111320),
                    provenance={
                        "source": "commons_gps_cluster",
                        "method": "centroid of geotagged references",
                        "refs": len(best_cell),
                        "radius_m": round(max_d),
                    },
                ))
                created += 1
        return {"sectors_created": created}

    def reassign_places(self, *, fallback_radius_m: int = 3000) -> dict[str, int]:
        """Reasigna referencias GPS al lugar más cercano (estricto, luego fallback).

        Estricto: radio del lugar semilla o 400 m para OSM. Si ninguna entra,
        fallback al lugar más cercano dentro de `fallback_radius_m` marcando la
        vista como `nearest_place_fallback` (honesto, no precisión inventada).
        """
        store = self.svc.store
        places = store.list_places(limit=100000)
        pts = [(p, p.lat, p.lon) for p in places if p.lat is not None and p.lon is not None]
        stats = {"reassigned": 0, "unassigned": 0, "views_created": 0}
        # frases distintivas (nombre/alias completos, len>=4): una palabra genérica
        # como "puente" NUNCA confirma identidad por sí sola
        seed_phrases: dict[str, set[str]] = {}
        for p in places:
            if (p.provenance or {}).get("source") == "seed" and p.lat is not None:
                phrases = {normalize_text(p.name)}
                phrases.update(normalize_text(a) for a in p.aliases)
                seed_phrases[p.place_id] = {t for t in phrases if len(t) >= 4}
        with store.batch():
            for ref in store.all_references(usable_only=True):
                if ref.gps_lat is None or ref.gps_lon is None:
                    continue
                # coincidencia de título con nombre/alias COMPLETO de una semilla
                # curada; si varias coinciden, gana la más cercana por GPS
                ref_norm = normalize_text(ref.title)
                matches = []
                for pid, phrases in seed_phrases.items():
                    if phrases and any(ph in ref_norm for ph in phrases):
                        seed_p = next(p for p in places if p.place_id == pid)
                        d = haversine_m(ref.gps_lat, ref.gps_lon, seed_p.lat, seed_p.lon)
                        if d <= 2500:
                            matches.append((d, seed_p))
                title_hit = None
                if matches:
                    matches.sort(key=lambda t: t[0])
                    title_hit = (matches[0][1], "title_match")
                if title_hit is not None:
                    chosen, method = title_hit
                    if ref.place_id != chosen.place_id:
                        ref.place_id = chosen.place_id
                        store.upsert_reference(ref)
                        stats["reassigned"] += 1
                        store.set_view_place(f"view_{ref.ref_id}", ref.place_id)
                    if store.get_view(f"view_{ref.ref_id}") is None:
                        store.upsert_view(View(
                            view_id=f"view_{ref.ref_id}", place_id=ref.place_id,
                            ref_id=ref.ref_id, label=ref.title[:120],
                            lat=ref.gps_lat, lon=ref.gps_lon,
                            calib_state="uncalibrated", method=method,
                        ))
                        stats["views_created"] += 1
                    continue
                best = None
                best_seed = None
                best_landmark = None
                best_fallback = None
                landmark_kinds = {"bridge", "mirador", "plaza", "park", "road_corridor",
                                  "river", "street", "intersection", "hub"}
                for p, plat, plon in pts:
                    d = haversine_m(ref.gps_lat, ref.gps_lon, plat, plon)
                    is_seed = (p.provenance or {}).get("source") == "seed"
                    radius = (
                        int((p.provenance or {}).get("assign_radius_m", 500))
                        if is_seed else 400
                    )
                    if is_seed and d <= radius and (best_seed is None or d < best_seed[1]):
                        best_seed = (p, d)
                    if not is_seed and d <= radius:
                        if best is None or d < best[1]:
                            best = (p, d)
                        if str(p.kind) in landmark_kinds and (
                            best_landmark is None or d < best_landmark[1]
                        ):
                            best_landmark = (p, d)
                    if d <= fallback_radius_m and (
                        best_fallback is None or d < best_fallback[1]
                    ):
                        best_fallback = (p, d)
                # las semillas curadas (puentes, avenidas, plazas) ganan sobre nodos OSM
                if best_seed is not None:
                    best = best_seed
                elif best_landmark is not None:
                    best = best_landmark
                chosen, method = (best, "nearest_in_radius") if best else (
                    (best_fallback, "nearest_place_fallback") if best_fallback else (None, None)
                )
                if chosen is None:
                    stats["unassigned"] += 1
                    continue
                if ref.place_id != chosen[0].place_id:
                    ref.place_id = chosen[0].place_id
                    store.upsert_reference(ref)
                    stats["reassigned"] += 1
                    store.set_view_place(f"view_{ref.ref_id}", ref.place_id)
                existing = store.get_view(f"view_{ref.ref_id}")
                if existing is None:
                    store.upsert_view(View(
                        view_id=f"view_{ref.ref_id}",
                        place_id=ref.place_id,
                        ref_id=ref.ref_id,
                        label=ref.title[:120],
                        lat=ref.gps_lat,
                        lon=ref.gps_lon,
                        calib_state="uncalibrated",
                        method=method,
                    ))
                    stats["views_created"] += 1
        self._recompute_coverage()
        return stats

    def build_scenarios(self) -> dict[str, int]:
        """Registra escenarios con identidad estable, versión, refs, capacidades.

        - photo_bg (verificado): la fotografía existe en disco con hash; sirve
          para composición que preserva sus píxeles. Capacidades reales:
          preserve_background_pixels=True, translación libre=False,
          rotación en el punto=False, profundidad desde foto única=False.
        - multiview (borrador/candidato): ≥5 vistas utilizables en ≤200 m con GPS
          (solapamiento comprobable por proximidad). NUNCA se declara
          reconstrucción validada: eso requiere COLMAP/nerfstudio en GPU (fase
          posterior).
        """
        from comfy_studio.atlas.models import Scenario, ScenarioKind, ScenarioStatus

        store = self.svc.store
        counts = {"photo_bg": 0, "multiview": 0}
        with store.batch():
            for p in store.list_places(limit=100000):
                views = [
                    v for v in store.views_for_place(p.place_id)
                    if v.usable and v.lat is not None and v.lon is not None
                    and (store.get_reference(v.ref_id) or _dummy_ref()).usable
                ]
                if not views:
                    continue
                refs = []
                for v in views:
                    r = store.get_reference(v.ref_id)
                    if r and r.usable and r.content_class == "fondo_urbano":
                        refs.append(r.ref_id)
                if not refs:
                    continue
                scn_id = f"scn_{p.place_id}_v1"
                store.upsert_scenario(Scenario(
                    scenario_id=scn_id,
                    place_id=p.place_id,
                    kind=ScenarioKind.PHOTO_BG,
                    version=1,
                    refs=refs,
                    transforms={
                        "crs": "WGS84",
                        "local_meters": "equirectangular en el origen del lugar",
                    },
                    capabilities={
                        "preserve_background_pixels": True,
                        "free_camera_translation": False,
                        "rotate_at_capture_point": False,
                        "depth_from_single_photo": False,
                        "source": "fotografía real georreferenciada",
                    },
                    status=ScenarioStatus.VERIFIED,
                ))
                counts["photo_bg"] += 1
                # multivista candidata: cluster GPS denso (solapamiento probable)
                if len(refs) >= 5:
                    lats = [v.lat for v in views]
                    lons = [v.lon for v in views]
                    spread = max(
                        haversine_m(min(lats), min(lons), max(lats), max(lons)), 1.0
                    )
                    if spread <= 200:
                        store.upsert_scenario(Scenario(
                            scenario_id=f"scn_{p.place_id}_mv_v1",
                            place_id=p.place_id,
                            kind=ScenarioKind.MULTIVIEW,
                            version=1,
                            refs=refs,
                            transforms={"crs": "WGS84", "local_meters": "equirectangular"},
                            capabilities={
                                "colmap_ready": True,
                                "overlap_evidence": (
                                    f"{len(refs)} vistas GPS en ≤{spread:.0f} m"
                                ),
                                "reconstruction_validated": False,
                                "note": (
                                    "dataset candidato; reconstrucción GPU pendiente "
                                    "(COLMAP/nerfstudio)"
                                ),
                            },
                            status=ScenarioStatus.DRAFT,
                        ))
                        counts["multiview"] += 1
        return counts


    def purge_library(self) -> dict[str, int]:
        """Audita TODAS las referencias adquiridas y clasifica su contenido.

       Sólo `fondo_urbano` es usable como fondo de plano. Las demás quedan
        registradas con su clase (historico, interior, detalle_macro,
        comida_objeto, retrato_persona, irrelevante, territorialmente_incompatible)
        y usable=0. Devuelve el recuento por clase.
        """
        store = self.svc.store
        counts: dict[str, int] = {}
        with store.batch():
            for ref in store.all_references(usable_only=False):
                gps_mun = None
                if ref.place_id:
                    pl = store.get_place(ref.place_id)
                    gps_mun = pl.municipality if pl else None
                cls = classify_reference(ref.title, ref.description, ref.capture_date, gps_mun)
                ref.content_class = cls
                if cls == CLASS_FONDO:
                    ref.usable = True
                    ref.reject_reason = None
                else:
                    ref.usable = False
                    ref.reject_reason = f"clase de contenido: {cls}"
                store.upsert_reference(ref)
                counts[cls] = counts.get(cls, 0) + 1
        self._recompute_coverage()
        return counts

    def mark_non_background_refs(self) -> int:
        """Marca como no utilizables como fondo las referencias de primer plano
        (animales, personas, vegetación macro...) ya adquiridas."""
        store = self.svc.store
        marked = 0
        with store.batch():
            for r in store.all_references(usable_only=True):
                text = (r.title + " " + (r.description or "")).lower()
                if _REJECT_TITLE.search(text):
                    r.usable = False
                    r.reject_reason = "primer plano / no usable como fondo de plano"
                    store.upsert_reference(r)
                    marked += 1
        return marked

    def _dedupe_phash(self) -> int:
        """Marca como no utilizables los duplicados perceptuales (mismo phash y lugar)."""
        store = self.svc.store
        removed = 0
        groups: dict[tuple[str | None, str], list] = {}
        for r in store.all_references(usable_only=True):
            if r.phash:
                groups.setdefault((r.place_id, r.phash), []).append(r)
        with store.batch():
            for members in groups.values():
                if len(members) < 2:
                    continue
                members.sort(key=lambda r: (r.quality, r.width or 0), reverse=True)
                for dup in members[1:]:
                    dup.usable = False
                    dup.reject_reason = "duplicado perceptual (phash) de " + members[0].ref_id
                    store.upsert_reference(dup)
                    removed += 1
        return removed

    def _recompute_coverage(self) -> None:
        store = self.svc.store
        with store.batch():
            self._recompute_coverage_inner(store)

    def _recompute_coverage_inner(self, store) -> None:  # type: ignore[no-untyped-def]
        # Cobertura honesta y unificada: la misma medición en vivo del servicio
        # (solo vistas usables, rumbos conocidos, licencias y fechas reales).
        for p in store.list_places(limit=100000):
            self.svc.recompute_coverage(p.place_id)

    def _write_attribution(self) -> Path:
        store = self.svc.store
        lines = [
            "# Atribuciones del Atlas audiovisual del Valle de Aburrá",
            "",
            "Generado automáticamente por la adquisición del atlas. Cada referencia",
            "conserva autor, licencia, fechas y procedencia; ver también licenses/*.txt.",
            "",
            "## Fuentes de datos geográficos",
            "",
        ]
        for s in store.list_sources():
            lines.append(f"- **{s.name}** — {s.license} — {s.url} — términos: {s.terms or 'n/d'}")
        lines += ["", "## Referencias fotográficas (Wikimedia Commons)", ""]
        for r in store.all_references(usable_only=True):
            lic = store.get_license(r.ref_id)
            lines.append(
                f"- `{r.ref_id}` — {r.title} — {r.author or 'autor desconocido'} — "
                f"{r.license} — captura: {r.capture_date or 'desconocida'} "
                f"({r.date_reliability}) — {r.source_page}"
                + (f" — licencia: {lic['license_url']}" if lic and lic.get("license_url") else "")
            )
        path = self.root / "ATTRIBUTION.md"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    # ------------------------------------------------------------------ fuentes a sources.json

    def write_sources_json(self) -> Path:
        path = self.root / "sources.json"
        path.write_text(
            json.dumps(
                {"generated_ts": time.time(),
                 "sources": [s.to_dict() for s in self.svc.store.list_sources()]},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        return path


#: valor cuando el punto queda fuera de todo límite municipal conocido
MUNICIPALITY_UNKNOWN = "desconocido"


#: clases de contenido de una referencia (auditoría de la biblioteca)
CLASS_FONDO = "fondo_urbano"
CLASS_HISTORICO = "historico"
CLASS_INTERIOR = "interior"
CLASS_MACRO = "detalle_macro"
CLASS_COMIDA = "comida_objeto"
CLASS_RETRATO = "retrato_persona"
CLASS_IRRELEVANTE = "irrelevante"
CLASS_INCOMPATIBLE = "territorialmente_incompatible"
CLASS_PENDIENTE = "pendiente"

_CLASS_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    (CLASS_MACRO, re.compile(
        r"abeja|insect|mariposa|butterfly|araña|arania|spider|avispa|hormiga|"
        r"flor|flores|flower|planta|hoja|leaf|macro|primer plano|detalle de|"
        r"plumaje|pelaje|ojo de", re.IGNORECASE)),
    (CLASS_COMIDA, re.compile(
        r"ajiaco|sancocho|bandeja|plato de|comida|food|meal|arepa|empanada|"
        r"tamal|mazamorra|postre|dessert|cerveza|coctel|bebida|menaje|cubiertos", re.IGNORECASE)),
    (CLASS_RETRATO, re.compile(
        r"persona|hombre|mujer|niño|nino|selfie|retrato|portrait|cara|rostro|"
        r"familia|grupo de|modelo|figurante", re.IGNORECASE)),
    (CLASS_INTERIOR, re.compile(
        r"\binterior\b|\bindoor\b|sala de|oficina|dentro de|habitación|"
        r"cocina|comedor|baño|bano", re.IGNORECASE)),
    (CLASS_IRRELEVANTE, re.compile(
        r"mapa|map\b|plan\b|plano|diagram|diagrama|logo|escudo|bandera|flag|"
        r"sello|stamp|poster|afiche|moneda|billete|dibujo|drawing|painting|"
        r"pintura|grabado|engraving|maqueta|render 3d", re.IGNORECASE)),
)

#: municipios de fuera del Valle de Aburrá que aparecen en títulos (territorialmente
#: incompatibles aunque tengan GPS dentro del bbox del valle)
_OTHER_MUNICIPALITIES = (
    "marinilla", "rionegro", "el carmen de viboral", "el carmen", "guarne",
    "la ceja", "el retiro", "santuario", "sonson", "abejorral", "caicedo",
    "ciudad bolivar", "venecia", "fredonia", "amaga", "santa bárbara",
    "santa barbara", "la unión", "la union", "santafé de antioquia", "san pedro",
    "copacabana norte", "girardot", "sabaneta antioquia? no",
)


def classify_reference(
    title: str, description: str | None, capture_date: str | None, gps_municipality: str | None
) -> str:
    """Clasifica el contenido real de una referencia (no sólo metadatos GPS/licencia)."""
    text = f"{title} {description or ''}".lower()
    for cls, pat in _CLASS_PATTERNS:
        if pat.search(text):
            return cls
    for other in _OTHER_MUNICIPALITIES:
        other_clean = other.rstrip("? no")
        if other_clean in text and other_clean != (gps_municipality or "").lower():
            return CLASS_INCOMPATIBLE
    m = re.search(r"(\d{4})", capture_date or "")
    if m and int(m.group(1)) < _HISTORICAL_YEAR:
        return CLASS_HISTORICO
    return CLASS_FONDO


def _municipality_for(lat: float, lon: float) -> str:
    """Municipio por bbox MÁS ESPECÍFICO (menor área primero).

    El rectángulo es un acelerador de búsqueda, NO la identidad municipal
    authoritative: quien no cae en ningún bbox queda "desconocido" (nunca se
    fuerza a Medellín), y quien cae queda marcado en provenance como
    heurístico hasta verificarse con límites verificables.
    """
    def area(b: tuple[float, float, float, float]) -> float:
        return (b[1] - b[0]) * (b[3] - b[2])

    for name, bbox in sorted(MUNICIPALITY_BBOX.items(), key=lambda kv: area(kv[1])):
        min_lat, max_lat, min_lon, max_lon = bbox
        if min_lat <= lat <= max_lat and min_lon <= lon <= max_lon:
            return name
    return MUNICIPALITY_UNKNOWN


def _quality_score(info: dict[str, Any], w: int, h: int) -> float:
    score = 0.5
    if w >= 3000:
        score += 0.2
    elif w >= 2000:
        score += 0.1
    if info.get("capture_date"):
        score += 0.2
    if info.get("gps_lat") is not None:
        score += 0.1
    return min(1.0, round(score, 3))


def run_acquisition(
    *,
    root: Path | None = None,
    sources: str = "all",
    max_refs: int = 650,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Punto de entrada del CLI: ejecuta las fuentes pedidas en orden."""
    from comfy_studio.atlas import default_atlas_root

    root = Path(root) if root else default_atlas_root()
    root.mkdir(parents=True, exist_ok=True)
    pipe = AcquisitionPipeline(root, max_refs=max_refs, dry_run=dry_run)
    pipe.svc.store.set_meta("atlas_version", ATLAS_VERSION)
    wanted = {"osm", "geomedellin", "commons"} if sources == "all" else {
        s.strip() for s in sources.split(",") if s.strip()
    }
    result: dict[str, Any] = {"root": str(root), "run_id": pipe.run_id, "sources": {}}
    pipe.ensure_seed_places()
    if "osm" in wanted:
        result["sources"]["osm"] = pipe.run_osm()
    if "geomedellin" in wanted:
        result["sources"]["geomedellin"] = pipe.run_geomedellin()
    if "commons" in wanted:
        result["sources"]["commons"] = pipe.run_commons()
    pipe.write_sources_json()
    result["stats"] = pipe.svc.store.stats()
    result["ok"] = all(
        (r.get("ok") or r.get("skipped")) for r in result["sources"].values()
    ) and result["sources"].get("commons", {}).get("ok", False)
    return result
