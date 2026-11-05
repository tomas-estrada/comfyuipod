"""Servicio de alto nivel del atlas: lo que consumen agente, API y planner.

Operaciones (equivalentes a listar/buscar lugares, resolver identidad, consultar
cobertura y vistas, seleccionar cámara, preparar un plano, validar fidelidad):

- search_places / resolve_place: NUNCA sustituye una ubicación explícita; si hay
  ambigüedad devuelve candidatos con estado "ambiguous".
- place_detail: lugar + cobertura medida + vistas.
- select_view: elige la vista respaldada más cercana a la cámara pedida; si la
  cámara pedida no está respaldada devuelve una limitación tipada + las vistas
  disponibles del mismo lugar.
- prepare_shot: resuelve lugar (explícito o selección activa o mejor cobertura),
  selecciona vista, verifica el archivo local y devuelve el contexto del plano.
- verify / stats / map_svg / export_manifest.
"""

from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from typing import Any

from comfy_studio.atlas import default_atlas_root
from comfy_studio.atlas.geo import (
    ABURRA_BBOX,
    haversine_m,
    normalize_text,
)
from comfy_studio.atlas.models import (
    Coverage,
    View,
)
from comfy_studio.atlas.store import AtlasStore

ATLAS_VERSION = "1.0.0"
SELECTION_FILE = "selection.json"


class AtlasUnavailableError(RuntimeError):
    """El atlas no está disponible (sin catálogo en la raíz indicada)."""


class AtlasService:
    """Fachada del atlas sobre un directorio raíz (catalog.db + files/ + ...)."""

    def __init__(self, root: Path | str | None = None, *, create: bool = False) -> None:
        self.root = Path(root) if root else default_atlas_root()
        self.files_root = self.root / "files"
        self.previews_root = self.root / "previews"
        self.licenses_root = self.root / "licenses"
        self._create = create
        self._lock = threading.RLock()
        self._store: AtlasStore | None = None

    @property
    def db_path(self) -> Path:
        return self.root / "catalog.db"

    @property
    def store(self) -> AtlasStore:
        with self._lock:
            if self._store is None:
                if not self.db_path.is_file():
                    if not self._create:
                        raise AtlasUnavailableError(
                            f"sin catálogo del atlas en {self.db_path} "
                            f"(STUDIO_ATLAS_DIR={self.root})"
                        )
                    self.root.mkdir(parents=True, exist_ok=True)
                self._store = AtlasStore(self.db_path)
            return self._store

    def available(self) -> bool:
        return self.db_path.is_file()

    def atlas_version(self) -> str:
        return self.store.get_meta("atlas_version") or ATLAS_VERSION

    # -- selección activa (estado del agente, recuperable) ---------------------

    def get_selection(self) -> dict[str, Any]:
        path = self.root / SELECTION_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def set_selection(self, place_id: str, view_id: str, *, note: str = "") -> dict[str, Any]:
        payload = {
            "place_id": place_id,
            "view_id": view_id,
            "note": note,
            "atlas_version": self.atlas_version(),
            "updated_ts": time.time(),
        }
        path = self.root / SELECTION_FILE
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return payload

    # -- búsqueda y resolución ---------------------------------------------------

    def search_places(
        self,
        *,
        q: str | None = None,
        municipality: str | None = None,
        environment: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        min_views: int = 0,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        store = self.store
        if q:
            places = store.search_places_fts(q, limit=200)
            norm = normalize_text(q)
            exact = store.get_place_by_norm_name(norm, municipality)
            seen: set[str] = set()
            merged = [p for p in exact if not (p.place_id in seen or seen.add(p.place_id))]
            for p in places:
                if p.place_id not in seen:
                    seen.add(p.place_id)
                    merged.append(p)
            # filtro adicional por municipio/kind sobre los resultados textuales
            if municipality:
                merged = [p for p in merged if p.municipality == municipality]
            if environment:
                merged = [p for p in merged if str(p.kind) == environment]
        else:
            kind = environment
            merged = store.list_places(
                municipality=municipality, kind=kind, bbox=bbox, limit=1000, offset=0
            )
        out: list[dict[str, Any]] = []
        scanned = 0
        for p in merged:
            if scanned >= offset + limit + offset and len(out) >= limit:
                break
            scanned += 1
            views = store.views_for_place(p.place_id)
            cov = store.get_coverage(p.place_id)
            if min_views and len(views) < min_views:
                continue
            if scanned <= offset:
                continue
            out.append(self._place_summary(p, views=len(views), coverage=cov))
            if len(out) >= limit:
                break
        return out

    def _place_summary(
        self, place, *, views: int, coverage: Coverage | None
    ) -> dict[str, Any]:
        return {
            "place_id": place.place_id,
            "name": place.name,
            "aliases": place.aliases,
            "municipality": place.municipality,
            "kind": str(place.kind),
            "sector": place.sector,
            "lat": place.lat,
            "lon": place.lon,
            "bounds": list(place.bounds) if place.bounds else None,
            "views_usable": views,
            "coverage": coverage.to_dict() if coverage else None,
        }

    def resolve_place(
        self, query: str, *, municipality: str | None = None
    ) -> dict[str, Any]:
        """Resuelve un nombre a un lugar. Nunca sustituye en silencio.

        Devuelve {"status": "confirmed"|"ambiguous"|"not_found", "place"?,
        "candidates"?}. Un match exacto de nombre normalizado gana; si hay varios
        lugares distintos con el mismo nombre (p.ej. parques repetidos) y no se
        desambigua por municipio/coordenadas, el estado es "ambiguous".
        """
        store = self.store
        norm = normalize_text(query)
        exact = store.get_place_by_norm_name(norm, municipality)
        if len(exact) == 1:
            return {"status": "confirmed", "place": self.place_detail(exact[0].place_id)}
        if len(exact) > 1:
            return {
                "status": "ambiguous",
                "candidates": [self._place_summary(p, views=len(store.views_for_place(p.place_id)),
                                                   coverage=store.get_coverage(p.place_id))
                               for p in exact],
                "hint": "Indica municipio o coordenadas para desambiguar",
            }
        # alias exacto
        alias_hits: list = []
        for p in store.list_places(municipality=municipality, limit=1000):
            if norm in [normalize_text(a) for a in p.aliases] or norm == normalize_text(p.name):
                alias_hits.append(p)
        if len(alias_hits) == 1:
            return {"status": "confirmed", "place": self.place_detail(alias_hits[0].place_id)}
        if len(alias_hits) > 1:
            return {
                "status": "ambiguous",
                "candidates": [self._place_summary(p, views=len(store.views_for_place(p.place_id)),
                                                   coverage=store.get_coverage(p.place_id))
                               for p in alias_hits],
            }
        # FTS como último recurso (parcial)
        fts = store.search_places_fts(query, limit=10)
        if municipality:
            fts = [p for p in fts if p.municipality == municipality]
        if len(fts) == 1:
            return {"status": "confirmed", "place": self.place_detail(fts[0].place_id),
                    "match": "parcial"}
        if fts:
            return {
                "status": "ambiguous",
                "candidates": [self._place_summary(p, views=len(store.views_for_place(p.place_id)),
                                                   coverage=store.get_coverage(p.place_id))
                               for p in fts],
            }
        return {"status": "not_found", "query": query}

    def place_detail(self, place_id: str) -> dict[str, Any]:
        store = self.store
        place = store.get_place(place_id)
        if place is None:
            raise KeyError(f"lugar desconocido: {place_id}")
        views = store.views_for_place(place_id)
        refs = store.references_for_place(place_id)
        cov = store.get_coverage(place_id)
        scenarios = store.scenarios_for_place(place_id)
        return {
            **self._place_summary(place, views=len(views), coverage=cov),
            "references_usable": len(refs),
            "views": [self._view_dict(v, store) for v in views],
            "scenarios": [s.to_dict() for s in scenarios],
            "provenance": place.provenance,
        }

    def _view_dict(self, view: View, store: AtlasStore) -> dict[str, Any]:
        ref = store.get_reference(view.ref_id)
        d = view.to_dict()
        d["preview"] = f"/studio/api/atlas/references/{view.ref_id}/preview"
        d["file"] = f"/studio/api/atlas/references/{view.ref_id}/file"
        d["license"] = ref.license if ref else None
        d["attribution"] = ref.attribution if ref else None
        d["capture_date"] = ref.capture_date if ref else None
        d["date_reliability"] = str(ref.date_reliability) if ref else None
        d["width"] = ref.width if ref else None
        d["height"] = ref.height if ref else None
        d["resolution_class"] = str(ref.resolution_class) if ref else None
        return d

    # -- selección de vista / cámara ----------------------------------------------

    def select_view(
        self,
        place_id: str,
        *,
        lat: float | None = None,
        lon: float | None = None,
        heading_deg: float | None = None,
        fov_deg: float | None = None,
    ) -> dict[str, Any]:
        """Selecciona la vista respaldada para una cámara pedida.

        Si no hay vistas: status="no_views". Si la vista elegida no respalda el
        heading pedido (desconocido), se devuelve limitation tipada con las vistas
        disponibles; nunca se inventa orientación.
        """
        store = self.store
        place = store.get_place(place_id)
        if place is None:
            raise KeyError(f"lugar desconocido: {place_id}")
        views = store.views_for_place(place_id)
        if not views:
            return {
                "status": "no_views",
                "place_id": place_id,
                "limitation": {"code": "no_usable_views",
                               "detail": "El lugar no tiene vistas fotográficas utilizables"},
                "available_views": [],
            }
        limitations: list[dict[str, Any]] = []
        chosen: View | None = None
        if lat is not None and lon is not None:
            best_d = None
            for v in views:
                if v.lat is None or v.lon is None:
                    continue
                d = haversine_m(lat, lon, v.lat, v.lon)
                if best_d is None or d < best_d:
                    best_d = d
                    chosen = v
            if chosen is None:
                chosen = views[0]
            if best_d is not None and best_d > 150:
                limitations.append({
                    "code": "camera_far_from_views",
                    "detail": f"La cámara pedida está a {best_d:.0f} m de la vista más cercana",
                    "distance_m": round(best_d, 1),
                })
        else:
            chosen = max(views, key=lambda v: (store.get_reference(v.ref_id).quality
                                                if store.get_reference(v.ref_id) else 0.0))
        if heading_deg is not None and chosen.heading_deg is None:
            limitations.append({
                "code": "heading_unknown",
                "detail": "La vista no tiene orientación de cámara registrada; "
                          "se usa la posición de captura, no un rumbo inventado",
            })
        if fov_deg is not None and chosen.fov_deg is None:
            limitations.append({
                "code": "fov_unknown",
                "detail": "FOV no estimado para esta vista (calibración pendiente)",
            })
        return {
            "status": "ok",
            "place_id": place_id,
            "view": self._view_dict(chosen, store),
            "limitations": limitations,
            "available_views": [self._view_dict(v, store) for v in views],
        }

    # -- prepare_shot ---------------------------------------------------------------

    def prepare_shot(
        self,
        *,
        place: str | None = None,
        place_id: str | None = None,
        view_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        heading_deg: float | None = None,
        prompt: str = "",
        verify_file: bool = True,
    ) -> dict[str, Any]:
        """Prepara un plano: lugar + vista + archivo local verificado + contexto.

        Resolución de lugar, en orden: place_id explícito > nombre `place` >
        selección activa > lugar con mejor cobertura. Devuelve el fondo local
        (ruta relativa dentro del atlas) y sus metadatos; nunca un placeholder.
        """
        store = self.store
        limitations: list[dict[str, Any]] = []
        resolved_place: dict[str, Any] | None = None
        if place_id:
            resolved_place = self.place_detail(place_id)
        elif place:
            res = self.resolve_place(place)
            if res["status"] == "confirmed":
                resolved_place = res["place"]
            elif res["status"] == "ambiguous":
                return {
                    "status": "ambiguous_place",
                    "candidates": res["candidates"],
                    "detail": "Hay varios lugares con ese nombre; elige uno explícito",
                }
            else:
                return {"status": "place_not_found", "query": place}
        else:
            sel = self.get_selection()
            if sel.get("place_id") and store.get_place(sel["place_id"]):
                resolved_place = self.place_detail(sel["place_id"])
                limitations.append({"code": "using_active_selection",
                                    "detail": "Sin ubicación en el prompt; se usa la vista "
                                              "seleccionada en el panel"})
            else:
                best = self.search_places(min_views=1, limit=1)
                if not best:
                    return {"status": "no_places_available"}
                best.sort(key=lambda p: p["views_usable"], reverse=True)
                chosen_place = best[0]
                resolved_place = self.place_detail(chosen_place["place_id"])
                limitations.append({
                    "code": "auto_selected_scene",
                    "detail": (
                        "Sin ubicación ni fondo; se eligió "
                        f"'{chosen_place['name']}' ({chosen_place['municipality']}) "
                        "por cobertura disponible"
                    ),
                })
        pid = resolved_place["place_id"]
        if view_id:
            view = store.get_view(view_id)
            if view is None or view.place_id != pid:
                return {"status": "view_not_found", "view_id": view_id, "place_id": pid}
            sel = self.select_view(pid, lat=lat, lon=lon, heading_deg=heading_deg)
            chosen = view
            limitations.extend(sel.get("limitations", []))
        else:
            sel = self.select_view(pid, lat=lat, lon=lon, heading_deg=heading_deg)
            if sel["status"] != "ok":
                return {"status": sel["status"], "place_id": pid,
                        "limitation": sel.get("limitation"),
                        "available_views": sel.get("available_views", [])}
            chosen = store.get_view(sel["view"]["view_id"])
            limitations.extend(sel.get("limitations", []))
        assert chosen is not None
        ref = store.get_reference(chosen.ref_id)
        if ref is None:
            return {"status": "reference_missing", "ref_id": chosen.ref_id}
        file_abs = (self.files_root / ref.file_path).resolve()
        try:
            file_abs.relative_to(self.files_root.resolve())
        except ValueError:
            return {"status": "unsafe_path", "ref_id": ref.ref_id}
        file_state = "present"
        if not file_abs.is_file():
            return {"status": "file_missing", "ref_id": ref.ref_id, "file_path": ref.file_path}
        if verify_file:
            from comfy_studio.atlas.store import sha256_file

            if sha256_file(file_abs) != ref.sha256:
                return {"status": "hash_mismatch", "ref_id": ref.ref_id}
        shot = {
            "status": "ok",
            "atlas_version": self.atlas_version(),
            "place_id": pid,
            "place": {
                "name": resolved_place["name"],
                "municipality": resolved_place["municipality"],
                "kind": resolved_place["kind"],
            },
            "view_id": chosen.view_id,
            "ref_id": ref.ref_id,
            "background_path": ref.file_path,  # relativo a files/
            "background_abs": str(file_abs),
            "file_state": file_state,
            "preview": f"/studio/api/atlas/references/{ref.ref_id}/preview",
            "camera": {
                "lat": chosen.lat,
                "lon": chosen.lon,
                "heading_deg": chosen.heading_deg,
                "fov_deg": chosen.fov_deg,
                "calib_state": str(chosen.calib_state),
                "method": chosen.method,
                "coordinate_system": "WGS84; metros locales via geo.latlon_to_xy",
            },
            "reference": {
                "title": ref.title,
                "author": ref.author,
                "license": ref.license,
                "license_url": ref.license_url,
                "attribution": ref.attribution,
                "capture_date": ref.capture_date,
                "date_reliability": str(ref.date_reliability),
                "width": ref.width,
                "height": ref.height,
                "resolution_class": str(ref.resolution_class),
                "sha256": ref.sha256,
                "source_page": ref.source_page,
            },
            "prompt": prompt,
            "limitations": limitations,
        }
        self.set_selection(pid, chosen.view_id, note=prompt[:200])
        return shot

    # -- verificación / stats / manifest / mapa --------------------------------------

    def verify(self, *, require_files: bool = True, check_hashes: bool = True) -> dict[str, Any]:
        store = self.store
        report = store.verify(
            self.files_root, require_files=require_files, check_hashes=check_hashes
        )
        report["atlas_version"] = self.atlas_version()
        report["root"] = str(self.root)
        report["stats"] = store.stats()
        return report

    def stats(self) -> dict[str, Any]:
        return self.store.stats()

    def export_manifest(self) -> dict[str, Any]:
        """Manifiesto con sha256 de los archivos locales del bundle."""
        from comfy_studio.atlas.store import sha256_file

        files: list[dict[str, Any]] = []
        if self.files_root.is_dir():
            for p in sorted(self.files_root.rglob("*")):
                if p.is_file():
                    files.append({
                        "path": str(p.relative_to(self.root)),
                        "sha256": sha256_file(p),
                        "bytes": p.stat().st_size,
                    })
        previews: list[dict[str, Any]] = []
        if self.previews_root.is_dir():
            for p in sorted(self.previews_root.rglob("*")):
                if p.is_file():
                    previews.append({
                        "path": str(p.relative_to(self.root)),
                        "sha256": sha256_file(p),
                        "bytes": p.stat().st_size,
                    })
        data_files: list[dict[str, Any]] = []
        for sub in ("osm", "geodata"):
            d = self.root / sub
            if d.is_dir():
                for p in sorted(d.rglob("*")):
                    if p.is_file():
                        data_files.append({
                            "path": str(p.relative_to(self.root)),
                            "sha256": sha256_file(p),
                            "bytes": p.stat().st_size,
                        })
        manifest = {
            "atlas_version": self.atlas_version(),
            "generated_ts": time.time(),
            "root": str(self.root),
            "stats": self.store.stats(),
            "sources": [s.to_dict() for s in self.store.list_sources()],
            "files": files,
            "previews": previews,
            "data": data_files,
            "counts": {
                "reference_files": len(files),
                "previews": len(previews),
                "data_files": len(data_files),
                "bytes_files": sum(f["bytes"] for f in files),
            },
        }
        return manifest

    def write_manifest(self) -> Path:
        manifest = self.export_manifest()
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def map_svg(self, width: int = 900, height: int = 700) -> str:
        """Mapa SVG local del Valle de Aburrá dibujado desde NUESTROS datos.

        Usa las vías del extracto OSM propio (osm/roads.geojson.gz) y los markers
        de lugares del catálogo. Sin tiles remotos ni CDN.
        """
        min_lat, max_lat, min_lon, max_lon = ABURRA_BBOX

        def project(lat: float, lon: float) -> tuple[float, float]:
            x = (lon - min_lon) / (max_lon - min_lon) * (width - 40) + 20
            y = (1 - (lat - min_lat) / (max_lat - min_lat)) * (height - 40) + 20
            return x, y

        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="Mapa del atlas del Valle de Aburrá">',
            f'<rect width="{width}" height="{height}" fill="#0e1420"/>',
        ]
        roads_path = self.root / "osm" / "roads.geojson.gz"
        n_roads = 0
        if roads_path.is_file():
            try:
                with gzip.open(roads_path, "rt", encoding="utf-8") as fh:
                    gj = json.load(fh)
                for feat in gj.get("features", []):
                    geom = feat.get("geometry") or {}
                    coords = geom.get("coordinates") or []
                    if geom.get("type") == "LineString":
                        pts = coords
                    elif geom.get("type") == "MultiLineString":
                        pts = [c for line in coords for c in line]
                    else:
                        continue
                    if not pts:
                        continue
                    d = "M" + " L".join(
                        f"{project(c[1], c[0])[0]:.1f},{project(c[1], c[0])[1]:.1f}" for c in pts
                    )
                    parts.append(
                        f'<path d="{d}" fill="none" stroke="#2c3e57" stroke-width="1.2"/>'
                    )
                    n_roads += 1
            except (OSError, json.JSONDecodeError, gzip.BadGzipFile):
                n_roads = 0
        store = self.store
        for p in store.list_places(limit=2000):
            if p.lat is None or p.lon is None:
                continue
            x, y = project(p.lat, p.lon)
            color = "#e8b23a" if str(p.kind) == "bridge" else "#4fc3f7"
            parts.append(
                f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}">'
                f"<title>{p.name} ({p.municipality}) [{p.kind}]</title></circle>"
            )
        parts.append(
            f'<text x="20" y="{height - 12}" fill="#8fa3bf" font-size="12" '
            f'font-family="sans-serif">'
            f"Atlas Aburrá v{self.atlas_version()} — {n_roads} tramos OSM locales, "
            f"{store.stats()['places']} lugares — datos propios, sin tiles remotos</text>"
        )
        parts.append("</svg>")
        return "".join(parts)


def load_service(root: Path | str | None = None) -> AtlasService:
    return AtlasService(root)
