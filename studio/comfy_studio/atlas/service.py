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
SELECTION_FILE = "selection.json"  # compatibilidad (proyecto default)


def _selection_file(project_id: str | None) -> str:
    pid = (project_id or "default").strip() or "default"
    return f"selection_{pid}.json"


#: cuadrantes de rumbo en orden canónico (0°=N, 90°=E).
_QUADRANT_ORDER: tuple[str, ...] = ("N", "E", "S", "W")


def heading_quadrant(heading_deg: float) -> str:
    """Cuadrante cardinal de un rumbo en grados (N/E/S/W)."""
    h = heading_deg % 360.0
    if h < 45.0 or h >= 315.0:
        return "N"
    if h < 135.0:
        return "E"
    if h < 225.0:
        return "S"
    return "W"


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

    def get_selection(self, project_id: str | None = None) -> dict[str, Any]:
        # primero la selección del proyecto; si no hay, la global por compatibilidad
        for fname in (_selection_file(project_id), SELECTION_FILE):
            path = self.root / fname
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data:
                    data["selection_file"] = fname
                    return data
            except (OSError, json.JSONDecodeError):
                continue
        return {}

    def set_selection(
        self, place_id: str, view_id: str, *, note: str = "", project_id: str | None = None
    ) -> dict[str, Any]:
        """Guarda la vista activa POR PROYECTO (no contamina otras producciones)."""
        payload = {
            "place_id": place_id,
            "view_id": view_id,
            "note": note,
            "project_id": (project_id or "default").strip() or "default",
            "atlas_version": self.atlas_version(),
            "updated_ts": time.time(),
        }
        fname = _selection_file(project_id)
        path = self.root / fname
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
        from comfy_studio.atlas.geo import bbox_intersects, in_bbox

        store = self.store
        if q:
            norm = normalize_text(q)
            exact = store.get_place_by_norm_name(norm, municipality)
            alias_exact = store.get_places_by_alias_norm(norm, municipality)
            places = store.search_places_fts(q, limit=200)
            seen: set[str] = set()
            # prioridad real: nombre exacto > alias exacto > rank FTS5
            merged = []
            for p in exact + alias_exact + places:
                if p.place_id not in seen:
                    seen.add(p.place_id)
                    merged.append(p)
            # filtros sobre los resultados textuales (incluye bbox si se pide)
            if municipality:
                merged = [p for p in merged if p.municipality == municipality]
            if environment:
                merged = [p for p in merged if str(p.kind) == environment]
            if bbox is not None:
                kept = []
                for p in merged:
                    if p.lat is not None and p.lon is not None:
                        if in_bbox(p.lat, p.lon, bbox):
                            kept.append(p)
                    elif p.bounds is not None:
                        if bbox_intersects(p.bounds, bbox):
                            kept.append(p)
                    # sin geometría: no se afirma que esté dentro
                merged = kept
        else:
            kind = environment
            merged = store.list_places(
                municipality=municipality, kind=kind, bbox=bbox, limit=1000, offset=0
            )
        # aplica min_views antes de paginar para que offset/limit sean honestos
        filtered: list = []
        for p in merged:
            views = self._usable_views(p.place_id)
            if min_views and len(views) < min_views:
                continue
            cov = self.coverage_for(p.place_id, views=views)
            filtered.append(self._place_summary(p, views=len(views), coverage=cov))
        return filtered[offset : offset + limit]

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
                "candidates": [self._place_summary(p, views=len(self._usable_views(p.place_id)),
                                                   coverage=self.coverage_for(p.place_id))
                               for p in exact],
                "hint": "Indica municipio o coordenadas para desambiguar",
            }
        # alias exacto (insensible a acentos vía normalize_text)
        alias_hits = store.get_places_by_alias_norm(norm, municipality)
        if len(alias_hits) == 1:
            return {"status": "confirmed", "place": self.place_detail(alias_hits[0].place_id)}
        if len(alias_hits) > 1:
            return {
                "status": "ambiguous",
                "candidates": [self._place_summary(p, views=len(self._usable_views(p.place_id)),
                                                   coverage=self.coverage_for(p.place_id))
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
                "candidates": [self._place_summary(p, views=len(self._usable_views(p.place_id)),
                                                   coverage=self.coverage_for(p.place_id))
                               for p in fts],
            }
        return {"status": "not_found", "query": query}

    def extract_place_hint(self, text: str) -> str | None:
        """Extrae del texto un lugar del atlas mencionado explícitamente.

        Coincidencia por nombre/alias completo (frase, len>=4) dentro del texto
        normalizado; gana la coincidencia más larga. Devuelve place_id o None.
        """
        norm = normalize_text(text)
        if not norm:
            return None
        best: tuple[str, int] | None = None
        for p in self.store.list_places(limit=100000):
            names = [normalize_text(p.name)]
            names.extend(normalize_text(a) for a in p.aliases)
            for n in names:
                if len(n) >= 4 and n in norm:
                    if best is None or len(n) > best[1]:
                        best = (p.place_id, len(n))
        return best[0] if best else None

    def place_detail(self, place_id: str) -> dict[str, Any]:
        store = self.store
        place = store.get_place(place_id)
        if place is None:
            raise KeyError(f"lugar desconocido: {place_id}")
        views = self._usable_views(place_id)
        refs = store.references_for_place(place_id)
        cov = self.coverage_for(place_id, views=views)
        scenarios = store.scenarios_for_place(place_id)
        return {
            **self._place_summary(place, views=len(views), coverage=cov),
            "references_usable": len(refs),
            "views": [self._view_dict(v, store) for v in views],
            "scenarios": [s.to_dict() for s in scenarios],
            "provenance": place.provenance,
        }

    # -- cobertura honesta (medida, no almacenada) ---------------------------------

    def coverage_for(self, place_id: str, views: list[View] | None = None) -> Coverage:
        """Cobertura medida en vivo desde vistas usables (nunca un valor guardado).

        Cuenta vistas usables reales, cuadrantes de rumbo cubiertos/faltantes
        (N/E/S/W sobre `heading_deg` conocido), licencias distintas de las
        referencias usables y fechas de captura mínima/máxima. Sin rumbos
        inventados: lo desconocido se cuenta como `heading_unknown`.
        """
        store = self.store
        if views is None:
            views = self._usable_views(place_id)
        headings = sorted(v.heading_deg for v in views if v.heading_deg is not None)
        unknown = sum(1 for v in views if v.heading_deg is None)
        covered = sorted({heading_quadrant(h) for h in headings}, key=_QUADRANT_ORDER.index)
        missing = [q for q in _QUADRANT_ORDER if q not in covered]
        licenses: dict[str, int] = {}
        dates: list[str] = []
        for v in views:
            ref = store.get_reference(v.ref_id)
            if ref is None:
                continue
            licenses[ref.license] = licenses.get(ref.license, 0) + 1
            if ref.capture_date:
                dates.append(ref.capture_date)
        dates.sort()
        return Coverage(
            place_id=place_id,
            views_observed=len(views),
            backed={
                "view_ids": [v.view_id for v in views],
                "headings_deg": headings,
                "quadrants_covered": covered,
                "licenses": licenses,
                "refs_usable": len(views),
            },
            unobserved={
                "quadrants_missing": missing,
                "heading_unknown": unknown,
            },
            evidence_newest=dates[-1] if dates else None,
            evidence_oldest=dates[0] if dates else None,
            confidence_basis=(
                f"{len(views)} vistas usables, {len(headings)} rumbos conocidos, "
                f"{len(licenses)} licencias"
            ),
        )

    def recompute_coverage(self, place_id: str) -> Coverage:
        """Mide la cobertura y la persiste (repara valores guardados obsoletos)."""
        cov = self.coverage_for(place_id)
        self.store.upsert_coverage(cov)
        return cov

    def _usable_views(self, place_id: str) -> list[View]:
        """Vistas de un lugar cuya referencia está usable (enviada en el bundle)."""
        out = []
        for v in self.store.views_for_place(place_id):
            ref = self.store.get_reference(v.ref_id)
            if (
                ref is not None and v.usable and ref.place_id == v.place_id
                and ref.usable
                and ref.content_class == "fondo_urbano"
                and str(ref.date_reliability) != "historical"
            ):
                out.append(v)
        return out

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

    #: bonus (resta al score) por calibración verificada: una cámara verificada
    #: prepara mejor una toma que una sin calibrar.
    _CALIB_BONUS: dict[str, float] = {
        "verified": -20.0, "estimated": -10.0, "uncalibrated": 0.0,
    }
    #: bonus por fiabilidad de fecha: captura real > solo subida > desconocida.
    _DATE_BONUS: dict[str, float] = {
        "captured": -10.0, "uploaded_only": -5.0, "unknown": 0.0, "historical": 0.0,
    }

    @classmethod
    def _score_candidate(
        cls,
        view: View,
        ref: Any | None,
        *,
        dist_m: float | None,
        heading_diff: float | None,
        heading_unknown: bool,
        fov_diff: float | None,
        fov_unknown: bool,
    ) -> float:
        """Score multicriterio de una vista (menor = mejor fondo para la toma).

        Suma distancia (m) + 2×dif_rumbo (°) + dif_FOV (°); la calidad pesa
        hasta 150 (decide entre vistas del mismo punto); lo desconocido
        penaliza neutro (mitad de la tolerancia) en vez de 0 ficticio;
        calibración, fecha de captura y resolución dan bonus acotados.
        """
        score = max(0.0, dist_m or 0.0)
        if heading_diff is not None:
            score += 2.0 * heading_diff
        elif heading_unknown:
            score += 22.5
        if fov_diff is not None:
            score += 1.0 * fov_diff
        elif fov_unknown:
            score += 7.5
        quality = ref.quality if ref is not None else 0.0
        score += (1.0 - min(max(quality, 0.0), 1.0)) * 150.0
        score += cls._CALIB_BONUS.get(str(view.calib_state), 0.0)
        score += cls._DATE_BONUS.get(
            str(ref.date_reliability) if ref is not None else "unknown", 0.0
        )
        width = ref.width if ref is not None and ref.width else 0
        score -= (min(width, 2048) / 2048.0) * 10.0
        return score

    def select_view(
        self,
        place_id: str,
        *,
        exact_view_id: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
        heading_deg: float | None = None,
        fov_deg: float | None = None,
        heading_tolerance_deg: float = 45.0,
        max_distance_m: float = 150.0,
    ) -> dict[str, Any]:
        """Selecciona la vista compatible con la cámara pedida.

        Compatibilidad real cuando hay datos: ángulo (rumbo), posición y FOV.
        - Rumbo pedido con rumbo conocido en la vista y diferencia > tolerancia:
          NO se acepta; se devuelve status="unsupported_camera" con la limitación
          tipada y las vistas disponibles del mismo lugar.
        - Entre las compatibles se elige por score multicriterio (menor = mejor):
          distancia + rumbo + FOV + calidad + calibración + fiabilidad de fecha
          + resolución; empate se rompe por view_id (determinista).
        - Desconocido sigue siendo desconocido: se devuelve como limitación
          (heading_unknown / fov_unknown), nunca como 0 ni como compatible.
        """
        store = self.store
        place = store.get_place(place_id)
        if place is None:
            raise KeyError(f"lugar desconocido: {place_id}")
        views = self._usable_views(place_id)
        if exact_view_id is not None:
            requested_view = store.get_view(exact_view_id)
            if requested_view is None or requested_view.place_id != place_id:
                return {
                    "status": "view_not_found",
                    "place_id": place_id,
                    "view_id": exact_view_id,
                    "available_views": [self._view_dict(x, store) for x in views],
                }
            views = [view for view in views if view.view_id == exact_view_id]
        if not views:
            return {
                "status": "no_views",
                "place_id": place_id,
                "limitation": {"code": "no_usable_views",
                               "detail": "El lugar no tiene vistas fotográficas utilizables"},
                "available_views": [],
            }

        def angular_diff(a: float, b: float) -> float:
            d = abs((a - b) % 360.0)
            return min(d, 360.0 - d)

        def evaluate(view: View) -> tuple[list[dict[str, Any]], float | None,
                                           float | None, float | None]:
            """(limitaciones, distancia_m, dif_rumbo, dif_fov) para la cámara pedida.

            `dif_rumbo`/`dif_fov` son None si no se pidió o si la vista no los
            tiene registrados (entonces hay limitación tipada, no un 0).
            """
            lims: list[dict[str, Any]] = []
            dist: float | None = None
            hdiff: float | None = None
            fdiff: float | None = None
            if (
                lat is not None and lon is not None
                and view.lat is not None and view.lon is not None
            ):
                dist = haversine_m(lat, lon, view.lat, view.lon)
                if dist > max_distance_m:
                    lims.append({
                        "code": "camera_far_from_views",
                        "detail": f"La cámara pedida está a {dist:.0f} m de la vista "
                                  f"(máx {max_distance_m:.0f} m)",
                        "distance_m": round(dist, 1),
                    })
            if heading_deg is not None:
                if view.heading_deg is None:
                    lims.append({
                        "code": "heading_unknown",
                        "detail": "La vista no tiene rumbo registrado; no se afirma compatibilidad",
                    })
                else:
                    hdiff = angular_diff(heading_deg, view.heading_deg)
                    if hdiff > heading_tolerance_deg:
                        lims.append({
                            "code": "heading_mismatch",
                            "detail": f"Rumbo pedido {heading_deg:.0f}° vs vista "
                                      f"{view.heading_deg:.0f}° (dif {hdiff:.0f}° > "
                                      f"{heading_tolerance_deg:.0f}°)",
                            "requested_deg": heading_deg,
                            "view_deg": view.heading_deg,
                            "diff_deg": round(hdiff, 1),
                        })
            if fov_deg is not None:
                if view.fov_deg is None:
                    lims.append({"code": "fov_unknown", "detail": "FOV no estimado en esta vista"})
                else:
                    fdiff = abs(fov_deg - view.fov_deg)
                    if fdiff > 15.0:
                        lims.append({
                            "code": "fov_mismatch",
                            "detail": f"FOV pedido {fov_deg:.0f}° vs vista {view.fov_deg:.0f}°",
                        })
            return lims, dist, hdiff, fdiff

        def hard_incompatible(lims: list[dict[str, Any]]) -> bool:
            return any(lim["code"] in ("heading_mismatch", "fov_mismatch") for lim in lims)

        def scored(view: View) -> tuple[float, View, list[dict[str, Any]]]:
            lims, dist, hdiff, fdiff = evaluate(view)
            ref = store.get_reference(view.ref_id)
            return (
                self._score_candidate(
                    view, ref, dist_m=dist, heading_diff=hdiff,
                    heading_unknown=(
                        heading_deg is not None and view.heading_deg is None
                    ),
                    fov_diff=fdiff,
                    fov_unknown=fov_deg is not None and view.fov_deg is None,
                ),
                view,
                lims,
            )

        # ranking: la compatible con mejor score; si ninguna lo es, la mejor
        # puntuada con sus limitaciones duras (unsupported_camera).
        compatibles = [s for s in (scored(v) for v in views) if not hard_incompatible(s[2])]
        if compatibles:
            compatibles.sort(key=lambda t: (t[0], t[1].view_id))
            _, chosen, chosen_lims = compatibles[0]
            return {
                "status": "ok",
                "place_id": place_id,
                "view": self._view_dict(chosen, store),
                "limitations": chosen_lims,
                "available_views": [self._view_dict(x, store) for x in views],
            }
        fallback = sorted((scored(v) for v in views), key=lambda t: (t[0], t[1].view_id))
        _, chosen, chosen_lims = fallback[0]
        return {
            "status": "unsupported_camera",
            "place_id": place_id,
            "view": self._view_dict(chosen, store),
            "limitations": chosen_lims,
            "available_views": [self._view_dict(x, store) for x in views],
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
        fov_deg: float | None = None,
        prompt: str = "",
        verify_file: bool = True,
        project_id: str | None = None,
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
            sel = self.get_selection(project_id)
            if sel.get("place_id") and store.get_place(sel["place_id"]):
                resolved_place = self.place_detail(sel["place_id"])
                limitations.append({"code": "using_active_selection",
                                    "detail": "Sin ubicación en el prompt; se usa la vista "
                                              "seleccionada en el panel"})
            else:
                # sin ubicación ni fondo: elegir escena COMPATIBLE con la tarea
                wanted_kinds = _task_kinds(prompt)
                best = self.search_places(min_views=1, limit=200)
                if wanted_kinds:
                    compat = [p for p in best if p["kind"] in wanted_kinds]
                    if compat:
                        best = compat
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
            sel = self.select_view(
                pid,
                exact_view_id=view_id,
                lat=lat,
                lon=lon,
                heading_deg=heading_deg,
                fov_deg=fov_deg,
            )
            if sel["status"] != "ok":
                return {**sel, "place_id": pid}
            chosen = store.get_view(sel["view"]["view_id"])
            limitations.extend(sel.get("limitations", []))
        else:
            sel = self.select_view(
                pid, lat=lat, lon=lon, heading_deg=heading_deg, fov_deg=fov_deg
            )
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
        self.set_selection(pid, chosen.view_id, note=prompt[:200], project_id=project_id)
        return shot

    # -- verificación / stats / manifest / mapa --------------------------------------

    def compose_over_background(
        self,
        *,
        background_abs: str,
        out_path: str,
        foreground_abs: str | None = None,
        scale: float = 0.35,
        position: str = "bottom-center",
    ) -> dict[str, Any]:
        """Composición CPU real sobre la fotografía del atlas.

        Pega un asset con canal alfa sobre el fondo observado y guarda el
        resultado; los píxeles FUERA de la región del asset quedan idénticos al
        fondo (se comprueba). Sin GPU ni red. Devuelve rutas y hashes.
        """
        from comfy_studio.atlas.store import sha256_file

        try:
            from PIL import Image  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Pillow no disponible para composición CPU") from exc
        bg_path = Path(background_abs)
        if not bg_path.is_file():
            raise FileNotFoundError(f"fondo ausente: {background_abs}")
        bg = Image.open(bg_path).convert("RGB")
        W, H = bg.size
        fg_region = None
        if foreground_abs:
            fg_path = Path(foreground_abs)
            if not fg_path.is_file():
                raise FileNotFoundError(f"asset de primer plano ausente: {foreground_abs}")
            fg = Image.open(fg_path).convert("RGBA")
            target_w = max(8, int(W * scale))
            ratio = target_w / fg.width
            fg = fg.resize((target_w, max(8, int(fg.height * ratio))), Image.LANCZOS)
            if position == "bottom-center":
                x = (W - fg.width) // 2
                y = H - fg.height - max(8, int(H * 0.04))
            elif position == "center":
                x, y = (W - fg.width) // 2, (H - fg.height) // 2
            else:
                x, y = 8, H - fg.height - 8
            bg.paste(fg, (x, y), fg)
            fg_region = (x, y, x + fg.width, y + fg.height)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        fmt = "PNG" if (foreground_abs and str(out).lower().endswith(".png")) else "JPEG"
        bg.save(out, fmt, quality=92) if fmt == "JPEG" else bg.save(out, fmt)
        # comprobación de preservación: fuera de la región del asset, píxeles iguales
        preserved = True
        if fg_region:
            check = Image.open(out).convert("RGB")
            x0, y0, x1, y1 = fg_region
            top = check.crop((0, 0, W, max(1, y0)))
            orig_top = Image.open(bg_path).convert("RGB").crop((0, 0, W, max(1, y0)))
            preserved = list(top.getdata()) == list(orig_top.getdata())
        return {
            "out_path": str(out),
            "out_sha256": sha256_file(out),
            "background_path": str(bg_path),
            "background_sha256": sha256_file(bg_path),
            "foreground_path": foreground_abs,
            "foreground_region": list(fg_region) if fg_region else None,
            "background_pixels_preserved_outside_foreground": preserved,
            "size": [W, H],
        }

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
        for sub in ("osm", "geodata", "examples"):
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


#: mapeo tarea -> entornos compatibles (la elección automática considera la tarea)
_TASK_KINDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("carro", ("road_corridor", "bridge", "intersection", "street")),
    ("vía", ("road_corridor", "bridge", "intersection", "street")),
    ("avenida", ("road_corridor", "street")),
    ("puente", ("bridge",)),
    ("mirador", ("mirador",)),
    ("vista", ("mirador", "park")),
    ("parque", ("park",)),
    ("río", ("river",)),
    ("rio", ("river",)),
    ("plaza", ("plaza",)),
)


def _task_kinds(prompt: str) -> tuple[str, ...]:
    """Entornos compatibles con la tarea descrita en el prompt."""
    norm = normalize_text(prompt)
    out: list[str] = []
    for word, kinds in _TASK_KINDS:
        if word in norm.split() or word in norm:
            for k in kinds:
                if k not in out:
                    out.append(k)
    return tuple(out)


def load_service(root: Path | str | None = None) -> AtlasService:
    return AtlasService(root)
