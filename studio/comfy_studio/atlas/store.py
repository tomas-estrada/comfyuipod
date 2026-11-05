"""Almacén persistente del atlas: SQLite con RTree (espacial) + FTS5 (texto).

Un solo archivo ``catalog.db`` en la raíz del atlas; los bytes viven aparte en
``files/`` y sólo se carga en memoria lo necesario por consulta. Todas las
escrituras pasan por un lock (el servidor es async pero sqlite es bloqueante);
las lecturas usan la misma conexión con `check_same_thread=False`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from comfy_studio.atlas.geo import normalize_text
from comfy_studio.atlas.models import (
    Coverage,
    Place,
    ProductionRecord,
    Reference,
    Scenario,
    SourceRecord,
    View,
)
from comfy_studio.atlas.schema import ensure_schema

SCHEMA_VERSION_KEY = "schema_version"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


class AtlasStore:
    """Acceso al catálogo del atlas (CRUD + consultas espaciales/textuales)."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._batch_depth = 0
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            ensure_schema(self._conn)

    def _commit(self) -> None:
        if self._batch_depth == 0:
            self._conn.commit()

    from contextlib import contextmanager

    @contextmanager
    def batch(self):  # type: ignore[no-untyped-def]
        """Agrupa escrituras en una sola transacción (bulk import)."""
        with self._lock:
            self._batch_depth += 1
            try:
                yield
            finally:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._commit()

    # -- places ---------------------------------------------------------------

    def upsert_place(self, place: Place) -> Place:
        place.updated_ts = time.time()
        name_norm = normalize_text(place.name)
        aliases_json = json.dumps(place.aliases, ensure_ascii=False)
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO places(
                       place_id, municipality, name, name_norm, kind, aliases, sector,
                       lat, lon, min_lat, max_lat, min_lon, max_lon,
                       osm_refs, provenance, created_ts, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(place_id) DO UPDATE SET
                       municipality=excluded.municipality,
                       name=excluded.name,
                       name_norm=excluded.name_norm,
                       kind=excluded.kind,
                       aliases=excluded.aliases,
                       sector=excluded.sector,
                       lat=excluded.lat, lon=excluded.lon,
                       min_lat=excluded.min_lat, max_lat=excluded.max_lat,
                       min_lon=excluded.min_lon, max_lon=excluded.max_lon,
                       osm_refs=excluded.osm_refs,
                       provenance=excluded.provenance,
                       updated_ts=excluded.updated_ts""",
                (
                    place.place_id,
                    place.municipality,
                    place.name,
                    name_norm,
                    str(place.kind),
                    aliases_json,
                    place.sector,
                    place.lat,
                    place.lon,
                    place.bounds[0] if place.bounds else place.lat,
                    place.bounds[1] if place.bounds else place.lat,
                    place.bounds[2] if place.bounds else place.lon,
                    place.bounds[3] if place.bounds else place.lon,
                    json.dumps(place.osm_refs, ensure_ascii=False),
                    json.dumps(place.provenance, ensure_ascii=False),
                    place.created_ts,
                    place.updated_ts,
                ),
            )
            self._conn.execute(
                "DELETE FROM places_rtree WHERE id=?", (cur.lastrowid,)
            )
            if place.lat is not None and place.lon is not None:
                self._conn.execute(
                    "INSERT INTO places_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                    " VALUES (?,?,?,?,?)",
                    (
                        cur.lastrowid,
                        place.bounds[0] if place.bounds else place.lat,
                        place.bounds[1] if place.bounds else place.lat,
                        place.bounds[2] if place.bounds else place.lon,
                        place.bounds[3] if place.bounds else place.lon,
                    ),
                )
            self._conn.execute("DELETE FROM places_fts WHERE rowid=?", (cur.lastrowid,))
            self._conn.execute(
                "INSERT INTO places_fts(rowid, name, aliases, municipality) VALUES (?,?,?,?)",
                (cur.lastrowid, place.name, " ".join(place.aliases), place.municipality),
            )
            self._commit()
            return self.get_place(place.place_id) or place

    def get_place(self, place_id: str) -> Place | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM places WHERE place_id=?", (place_id,)
            ).fetchone()
            return _row_to_place(row) if row else None

    def get_place_by_norm_name(
        self, name_norm: str, municipality: str | None = None
    ) -> list[Place]:
        with self._lock:
            if municipality:
                rows = self._conn.execute(
                    "SELECT * FROM places WHERE name_norm=? AND municipality=?",
                    (name_norm, municipality),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM places WHERE name_norm=?", (name_norm,)
                ).fetchall()
            return [_row_to_place(r) for r in rows]

    def search_places_fts(self, query: str, limit: int = 20) -> list[Place]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT p.* FROM places_fts f JOIN places p ON p.id=f.rowid "
                    "WHERE places_fts MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(query), limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            return [_row_to_place(r) for r in rows]

    def list_places(
        self,
        *,
        municipality: str | None = None,
        kind: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Place]:
        sql = "SELECT p.* FROM places p"
        conds: list[str] = []
        params: list[Any] = []
        if bbox is not None:
            sql += " JOIN places_rtree r ON r.id=p.id"
            min_lat, max_lat, min_lon, max_lon = bbox
            conds.append("r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ?")
            params += [min_lat, max_lat, min_lon, max_lon]
        if municipality:
            conds.append("p.municipality=?")
            params.append(municipality)
        if kind:
            conds.append("p.kind=?")
            params.append(kind)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY p.municipality, p.name LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_place(r) for r in rows]

    def nearest_places(
        self, lat: float, lon: float, radius_m: float, *, limit: int = 20
    ) -> list[tuple[Place, float]]:
        from comfy_studio.atlas.geo import bbox_around, haversine_m

        bbox = bbox_around(lat, lon, radius_m)
        min_lat, max_lat, min_lon, max_lon = bbox
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.*, r.min_lat, r.max_lat, r.min_lon, r.max_lon FROM places p "
                "JOIN places_rtree r ON r.id=p.id "
                "WHERE r.max_lat >= ? AND r.min_lat <= ? AND r.max_lon >= ? AND r.min_lon <= ?",
                (min_lat, max_lat, min_lon, max_lon),
            ).fetchall()
        out: list[tuple[Place, float]] = []
        for row in rows:
            place = _row_to_place(row)
            if place.lat is None or place.lon is None:
                continue
            d = haversine_m(lat, lon, place.lat, place.lon)
            if d <= radius_m:
                out.append((place, d))
        out.sort(key=lambda t: t[1])
        return out[:limit]

    # -- references -------------------------------------------------------------

    def upsert_reference(self, ref: Reference) -> Reference:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO atlas_refs(
                       ref_id, place_id, title, file_path, sha256, source, source_page,
                       source_file_url, sha1_source, author, license, license_url, attribution,
                       capture_date, upload_date, date_reliability, fetched_ts, width, height,
                       native_width, native_height, bytes, mime, resolution_class,
                       derived_from_url, gps_lat, gps_lon,
                       heading_deg, intrinsics, environment, description, quality, usable,
                       reject_reason, phash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ref_id) DO UPDATE SET
                       place_id=excluded.place_id, title=excluded.title,
                       file_path=excluded.file_path, sha256=excluded.sha256,
                       source=excluded.source, source_page=excluded.source_page,
                       source_file_url=excluded.source_file_url,
                       sha1_source=excluded.sha1_source, author=excluded.author,
                       license=excluded.license, license_url=excluded.license_url,
                       attribution=excluded.attribution, capture_date=excluded.capture_date,
                       upload_date=excluded.upload_date,
                       date_reliability=excluded.date_reliability, fetched_ts=excluded.fetched_ts,
                       width=excluded.width, height=excluded.height, bytes=excluded.bytes,
                       mime=excluded.mime, resolution_class=excluded.resolution_class,
                       derived_from_url=excluded.derived_from_url, gps_lat=excluded.gps_lat,
                       gps_lon=excluded.gps_lon, heading_deg=excluded.heading_deg,
                       intrinsics=excluded.intrinsics, environment=excluded.environment,
                       description=excluded.description, quality=excluded.quality,
                       usable=excluded.usable, reject_reason=excluded.reject_reason,
                       phash=excluded.phash""",
                (
                    ref.ref_id,
                    ref.place_id,
                    ref.title,
                    ref.file_path,
                    ref.sha256,
                    ref.source,
                    ref.source_page,
                    ref.source_file_url,
                    ref.sha1_source,
                    ref.author,
                    ref.license,
                    ref.license_url,
                    ref.attribution,
                    ref.capture_date,
                    ref.upload_date,
                    str(ref.date_reliability),
                    ref.fetched_ts,
                    ref.width,
                    ref.height,
                    ref.native_width,
                    ref.native_height,
                    ref.bytes,
                    ref.mime,
                    str(ref.resolution_class),
                    ref.derived_from_url,
                    ref.gps_lat,
                    ref.gps_lon,
                    ref.heading_deg,
                    json.dumps(ref.intrinsics) if ref.intrinsics else None,
                    str(ref.environment) if ref.environment else None,
                    ref.description,
                    ref.quality,
                    1 if ref.usable else 0,
                    ref.reject_reason,
                    ref.phash,
                ),
            )
            rid = cur.lastrowid
            self._conn.execute("DELETE FROM refs_rtree WHERE id=?", (rid,))
            if ref.gps_lat is not None and ref.gps_lon is not None:
                self._conn.execute(
                    "INSERT INTO refs_rtree(id, min_lat, max_lat, min_lon, max_lon)"
                    " VALUES (?,?,?,?,?)",
                    (rid, ref.gps_lat, ref.gps_lat, ref.gps_lon, ref.gps_lon),
                )
            self._conn.execute("DELETE FROM refs_fts WHERE rowid=?", (rid,))
            self._conn.execute(
                "INSERT INTO refs_fts(rowid, title, author, description) VALUES (?,?,?,?)",
                (rid, ref.title, ref.author or "", ref.description or ""),
            )
            self._commit()
            return self.get_reference(ref.ref_id) or ref

    def get_reference(self, ref_id: str) -> Reference | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM atlas_refs WHERE ref_id=?", (ref_id,)
            ).fetchone()
            return _row_to_reference(row) if row else None

    def get_reference_by_sha1(self, sha1: str) -> Reference | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM atlas_refs WHERE sha1_source=?", (sha1,)
            ).fetchone()
            return _row_to_reference(row) if row else None

    def references_for_place(self, place_id: str, *, usable_only: bool = True) -> list[Reference]:
        sql = "SELECT * FROM atlas_refs WHERE place_id=?"
        params: list[Any] = [place_id]
        if usable_only:
            sql += " AND usable=1"
        sql += " ORDER BY quality DESC, fetched_ts DESC"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [_row_to_reference(r) for r in rows]

    def nearest_references(
        self, lat: float, lon: float, radius_m: float, *, usable_only: bool = True, limit: int = 50
    ) -> list[tuple[Reference, float]]:
        from comfy_studio.atlas.geo import bbox_around, haversine_m

        bbox = bbox_around(lat, lon, radius_m)
        min_lat, max_lat, min_lon, max_lon = bbox
        sql = (
            "SELECT r.* FROM atlas_refs r JOIN refs_rtree t ON t.id=r.id "
            "WHERE t.max_lat >= ? AND t.min_lat <= ? AND t.max_lon >= ? AND t.min_lon <= ?"
        )
        params: list[Any] = [min_lat, max_lat, min_lon, max_lon]
        if usable_only:
            sql += " AND r.usable=1"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out: list[tuple[Reference, float]] = []
        for row in rows:
            ref = _row_to_reference(row)
            if ref.gps_lat is None or ref.gps_lon is None:
                continue
            d = haversine_m(lat, lon, ref.gps_lat, ref.gps_lon)
            if d <= radius_m:
                out.append((ref, d))
        out.sort(key=lambda t: t[1])
        return out[:limit]

    def search_references_fts(self, query: str, limit: int = 30) -> list[Reference]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    "SELECT r.* FROM refs_fts f JOIN atlas_refs r ON r.id=f.rowid "
                    "WHERE refs_fts MATCH ? ORDER BY rank LIMIT ?",
                    (_fts_query(query), limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            return [_row_to_reference(r) for r in rows]

    def all_references(self, *, usable_only: bool = False, limit: int = 100000) -> list[Reference]:
        sql = "SELECT * FROM atlas_refs"
        if usable_only:
            sql += " WHERE usable=1"
        sql += " ORDER BY ref_id LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (limit,)).fetchall()
            return [_row_to_reference(r) for r in rows]

    # -- views / coverage / scenarios -------------------------------------------

    def upsert_view(self, view: View) -> View:
        with self._lock:
            self._conn.execute(
                """INSERT INTO views(view_id, place_id, ref_id, label, lat, lon, heading_deg,
                       fov_deg, calib_state, method, usable, notes, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(view_id) DO UPDATE SET
                       place_id=excluded.place_id, ref_id=excluded.ref_id,
                       label=excluded.label, lat=excluded.lat, lon=excluded.lon,
                       heading_deg=excluded.heading_deg, fov_deg=excluded.fov_deg,
                       calib_state=excluded.calib_state, method=excluded.method,
                       usable=excluded.usable, notes=excluded.notes""",
                (
                    view.view_id,
                    view.place_id,
                    view.ref_id,
                    view.label,
                    view.lat,
                    view.lon,
                    view.heading_deg,
                    view.fov_deg,
                    str(view.calib_state),
                    view.method,
                    1 if view.usable else 0,
                    view.notes,
                    view.created_ts,
                ),
            )
            self._commit()
            return self.get_view(view.view_id) or view

    def get_view(self, view_id: str) -> View | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM views WHERE view_id=?", (view_id,)).fetchone()
            return _row_to_view(row) if row else None

    def set_view_place(self, view_id: str, place_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE views SET place_id=? WHERE view_id=?", (place_id, view_id)
            )
            self._commit()

    def views_for_place(self, place_id: str, *, usable_only: bool = True) -> list[View]:
        sql = "SELECT * FROM views WHERE place_id=?"
        if usable_only:
            sql += " AND usable=1"
        sql += " ORDER BY created_ts"
        with self._lock:
            rows = self._conn.execute(sql, (place_id,)).fetchall()
            return [_row_to_view(r) for r in rows]

    def upsert_coverage(self, cov: Coverage) -> None:
        cov.updated_ts = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO coverage(place_id, views_observed, backed, unobserved,
                       evidence_newest, evidence_oldest, confidence_basis, updated_ts)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(place_id) DO UPDATE SET
                       views_observed=excluded.views_observed, backed=excluded.backed,
                       unobserved=excluded.unobserved,
                       evidence_newest=excluded.evidence_newest,
                       evidence_oldest=excluded.evidence_oldest,
                       confidence_basis=excluded.confidence_basis,
                       updated_ts=excluded.updated_ts""",
                (
                    cov.place_id,
                    cov.views_observed,
                    json.dumps(cov.backed, ensure_ascii=False),
                    json.dumps(cov.unobserved, ensure_ascii=False),
                    cov.evidence_newest,
                    cov.evidence_oldest,
                    cov.confidence_basis,
                    cov.updated_ts,
                ),
            )
            self._commit()

    def get_coverage(self, place_id: str) -> Coverage | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM coverage WHERE place_id=?", (place_id,)
            ).fetchone()
            return _row_to_coverage(row) if row else None

    def upsert_scenario(self, sc: Scenario) -> Scenario:
        with self._lock:
            self._conn.execute(
                """INSERT INTO scenarios(scenario_id, place_id, kind, version, refs,
                       geometry_path, transforms, capabilities, status, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(scenario_id) DO UPDATE SET
                       place_id=excluded.place_id, kind=excluded.kind,
                       version=excluded.version, refs=excluded.refs,
                       geometry_path=excluded.geometry_path,
                       transforms=excluded.transforms,
                       capabilities=excluded.capabilities, status=excluded.status""",
                (
                    sc.scenario_id,
                    sc.place_id,
                    str(sc.kind),
                    sc.version,
                    json.dumps(sc.refs, ensure_ascii=False),
                    sc.geometry_path,
                    json.dumps(sc.transforms, ensure_ascii=False),
                    json.dumps(sc.capabilities, ensure_ascii=False),
                    str(sc.status),
                    sc.created_ts,
                ),
            )
            self._commit()
            return sc

    def scenarios_for_place(self, place_id: str) -> list[Scenario]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM scenarios WHERE place_id=? ORDER BY version DESC",
                (place_id,),
            ).fetchall()
            return [_row_to_scenario(r) for r in rows]

    # -- productions / sources / acquisition ------------------------------------

    def record_production(self, prod: ProductionRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO productions(prod_id, atlas_version, place_id, view_id,
                       scenario_id, camera, params, result, created_ts)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(prod_id) DO UPDATE SET
                       atlas_version=excluded.atlas_version, place_id=excluded.place_id,
                       view_id=excluded.view_id, scenario_id=excluded.scenario_id,
                       camera=excluded.camera, params=excluded.params,
                       result=excluded.result""",
                (
                    prod.prod_id,
                    prod.atlas_version,
                    prod.place_id,
                    prod.view_id,
                    prod.scenario_id,
                    json.dumps(prod.camera, ensure_ascii=False),
                    json.dumps(prod.params, ensure_ascii=False),
                    json.dumps(prod.result, ensure_ascii=False),
                    prod.created_ts,
                ),
            )
            self._commit()

    def list_productions(self, limit: int = 100) -> list[ProductionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM productions ORDER BY created_ts DESC LIMIT ?", (limit,)
            ).fetchall()
            return [_row_to_production(r) for r in rows]

    def upsert_source(self, src: SourceRecord) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO sources(source_id, name, kind, url, license, license_url,
                       terms, extract_date, sha256, fetched_ts, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(source_id) DO UPDATE SET
                       name=excluded.name, kind=excluded.kind, url=excluded.url,
                       license=excluded.license, license_url=excluded.license_url,
                       terms=excluded.terms, extract_date=excluded.extract_date,
                       sha256=excluded.sha256, fetched_ts=excluded.fetched_ts,
                       notes=excluded.notes""",
                (
                    src.source_id,
                    src.name,
                    src.kind,
                    src.url,
                    src.license,
                    src.license_url,
                    src.terms,
                    src.extract_date,
                    src.sha256,
                    src.fetched_ts,
                    src.notes,
                ),
            )
            self._commit()

    def list_sources(self) -> list[SourceRecord]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM sources ORDER BY source_id").fetchall()
            return [_row_to_source(r) for r in rows]

    def set_license(self, ref_id: str, license_: str, license_url: str | None,
                    attribution: str | None, license_text_path: str | None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO licenses(
                       ref_id, license, license_url, attribution, license_text_path)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(ref_id) DO UPDATE SET
                       license=excluded.license, license_url=excluded.license_url,
                       attribution=excluded.attribution,
                       license_text_path=excluded.license_text_path""",
                (ref_id, license_, license_url, attribution, license_text_path),
            )
            self._commit()

    def get_license(self, ref_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM licenses WHERE ref_id=?", (ref_id,)
            ).fetchone()
            return dict(row) if row else None

    # -- acquisition resumable ----------------------------------------------------

    def begin_run(self, run_id: str, source: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO acq_runs(run_id, source, started_ts, status)"
                " VALUES (?,?,?, 'running')",
                (run_id, source, time.time()),
            )
            self._commit()

    def end_run(self, run_id: str, source: str, status: str, stats: dict[str, Any],
                error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE acq_runs SET finished_ts=?, status=?, stats=?, error=? "
                "WHERE run_id=? AND source=?",
                (time.time(), status, json.dumps(stats, ensure_ascii=False), error, run_id, source),
            )
            self._commit()

    def acq_item(self, run_id: str, source: str, key: str, status: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO acq_items(run_id, source, key, status, detail, ts)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(run_id, source, key) DO UPDATE SET
                       status=excluded.status, detail=excluded.detail, ts=excluded.ts""",
                (run_id, source, key, status, detail[:500], time.time()),
            )
            self._commit()

    def acq_item_status(self, run_id: str, source: str, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM acq_items WHERE run_id=? AND source=? AND key=?",
                (run_id, source, key),
            ).fetchone()
            return row[0] if row else None

    def count_done_items(self, run_id: str, source: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM acq_items WHERE run_id=? AND source=? AND status='done'",
                (run_id, source),
            ).fetchone()
            return int(row[0]) if row else 0

    # -- stats / verify -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        with self._lock:
            places = self._conn.execute("SELECT COUNT(*) FROM places").fetchone()[0]
            refs_total = self._conn.execute("SELECT COUNT(*) FROM atlas_refs").fetchone()[0]
            refs_usable = self._conn.execute(
                "SELECT COUNT(*) FROM atlas_refs WHERE usable=1"
            ).fetchone()[0]
            views = self._conn.execute(
                "SELECT COUNT(*) FROM views v JOIN atlas_refs r ON r.ref_id=v.ref_id "
                "WHERE v.usable=1 AND r.usable=1"
            ).fetchone()[0]
            places_with_views = self._conn.execute(
                "SELECT COUNT(DISTINCT v.place_id) FROM views v "
                "JOIN atlas_refs r ON r.ref_id=v.ref_id "
                "WHERE v.usable=1 AND r.usable=1"
            ).fetchone()[0]
            by_mun = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT municipality, COUNT(*) FROM places "
                    "GROUP BY municipality ORDER BY 2 DESC"
                ).fetchall()
            }
            by_kind = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) FROM places GROUP BY kind ORDER BY 2 DESC"
                ).fetchall()
            }
            refs_by_mun = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT p.municipality, COUNT(*) FROM atlas_refs rf "
                    "JOIN places p ON p.place_id=rf.place_id WHERE rf.usable=1 "
                    "GROUP BY p.municipality ORDER BY 2 DESC"
                ).fetchall()
            }
            by_date_rel = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT date_reliability, COUNT(*) FROM atlas_refs WHERE usable=1 "
                    "GROUP BY date_reliability"
                ).fetchall()
            }
            by_license = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT license, COUNT(*) FROM atlas_refs WHERE usable=1 "
                    "GROUP BY license ORDER BY 2 DESC"
                ).fetchall()
            }
            scenarios = self._conn.execute("SELECT COUNT(*) FROM scenarios").fetchone()[0]
            productions = self._conn.execute("SELECT COUNT(*) FROM productions").fetchone()[0]
        return {
            "places": places,
            "places_with_usable_views": places_with_views,
            "references_total": refs_total,
            "references_usable": refs_usable,
            "views_usable": views,
            "scenarios": scenarios,
            "productions": productions,
            "places_by_municipality": by_mun,
            "places_by_kind": by_kind,
            "references_by_municipality": refs_by_mun,
            "references_by_date_reliability": by_date_rel,
            "references_by_license": by_license,
        }

    def verify(self, files_root: Path, *, require_files: bool = True,
               check_hashes: bool = True, limit: int = 100000) -> dict[str, Any]:
        """Verificación de integridad del catálogo contra los archivos en disco.

        Comprueba: archivo existe, tamaño, sha256 (opcional), dimensiones
        decodificables que coinciden, licencia registrada. Devuelve un reporte
        con errores tipados; `ok` es False si hay cualquier error.
        """
        from comfy_studio.atlas.imaging import image_dimensions, is_raster_mime, sniff_mime

        errors: list[dict[str, Any]] = []
        warnings: list[str] = []
        refs = self.all_references(usable_only=True, limit=limit)
        checked = 0
        for ref in refs:
            checked += 1
            fpath = (files_root / ref.file_path).resolve()
            try:
                fpath.relative_to(files_root.resolve())
            except ValueError:
                errors.append({"ref_id": ref.ref_id, "error": "file_path escapa de files/"})
                continue
            if not fpath.is_file():
                if require_files:
                    errors.append({"ref_id": ref.ref_id, "error": "archivo ausente",
                                   "file_path": ref.file_path})
                else:
                    warnings.append(f"{ref.ref_id}: archivo ausente (permitido)")
                continue
            size = fpath.stat().st_size
            if ref.bytes is not None and size != ref.bytes:
                errors.append({"ref_id": ref.ref_id, "error": "tamaño no coincide",
                               "expected": ref.bytes, "actual": size})
            if check_hashes:
                digest = sha256_file(fpath)
                if digest != ref.sha256:
                    errors.append({"ref_id": ref.ref_id, "error": "sha256 no coincide",
                                   "expected": ref.sha256, "actual": digest})
            mime = sniff_mime(fpath)
            if mime is None:
                errors.append({"ref_id": ref.ref_id, "error": "MIME no reconocible"})
            elif ref.mime and mime != ref.mime and not (
                mime == "image/jpg" or ref.mime == "image/jpg"
            ):
                if not (mime == "image/jpeg" and ref.mime == "image/jpg"):
                    errors.append({"ref_id": ref.ref_id, "error": "MIME no coincide",
                                   "expected": ref.mime, "actual": mime})
            if mime and is_raster_mime(mime):
                dims = image_dimensions(fpath)
                if dims is None:
                    errors.append({"ref_id": ref.ref_id, "error": "imagen no decodificable"})
                elif ref.width and ref.height and dims != (ref.width, ref.height):
                    errors.append({"ref_id": ref.ref_id, "error": "dimensiones no coinciden",
                                   "expected": [ref.width, ref.height], "actual": list(dims)})
            lic = self.get_license(ref.ref_id)
            if lic is None:
                errors.append({"ref_id": ref.ref_id, "error": "licencia no registrada"})
            elif not lic.get("license") or lic["license"] == "UNKNOWN":
                errors.append({"ref_id": ref.ref_id, "error": "licencia desconocida"})
        return {
            "ok": not errors,
            "checked_references": checked,
            "errors": errors[:500],
            "error_count": len(errors),
            "warnings": warnings[:100],
        }


# ---------------------------------------------------------------------------
# Conversión fila <-> modelo
# ---------------------------------------------------------------------------


def _fts_query(query: str) -> str:
    """Convierte texto libre en una consulta FTS5 segura (AND de tokens)."""
    tokens = [t for t in normalize_text(query).split() if t]
    if not tokens:
        return '""'
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in tokens)


def _row_to_place(row: sqlite3.Row) -> Place:
    from comfy_studio.atlas.models import Environment

    bounds = None
    if row["min_lat"] is not None and row["lat"] is not None:
        bounds = (row["min_lat"], row["max_lat"], row["min_lon"], row["max_lon"])
    return Place(
        place_id=row["place_id"],
        municipality=row["municipality"],
        name=row["name"],
        kind=Environment(row["kind"]),
        aliases=json.loads(row["aliases"] or "[]"),
        sector=row["sector"],
        lat=row["lat"],
        lon=row["lon"],
        bounds=bounds,
        osm_refs=json.loads(row["osm_refs"] or "[]"),
        provenance=json.loads(row["provenance"] or "{}"),
        created_ts=row["created_ts"],
        updated_ts=row["updated_ts"],
    )


def _row_to_reference(row: sqlite3.Row) -> Reference:
    from comfy_studio.atlas.models import DateReliability, Environment, ResolutionClass

    return Reference(
        ref_id=row["ref_id"],
        title=row["title"],
        file_path=row["file_path"],
        sha256=row["sha256"],
        source=row["source"],
        place_id=row["place_id"],
        source_page=row["source_page"],
        source_file_url=row["source_file_url"],
        sha1_source=row["sha1_source"],
        author=row["author"],
        license=row["license"],
        license_url=row["license_url"],
        attribution=row["attribution"],
        capture_date=row["capture_date"],
        upload_date=row["upload_date"],
        date_reliability=DateReliability(row["date_reliability"]),
        fetched_ts=row["fetched_ts"],
        width=row["width"],
        height=row["height"],
        native_width=row["native_width"] if "native_width" in row.keys() else None,
        native_height=row["native_height"] if "native_height" in row.keys() else None,
        bytes=row["bytes"],
        mime=row["mime"],
        resolution_class=ResolutionClass(row["resolution_class"]),
        derived_from_url=row["derived_from_url"],
        gps_lat=row["gps_lat"],
        gps_lon=row["gps_lon"],
        heading_deg=row["heading_deg"],
        intrinsics=json.loads(row["intrinsics"]) if row["intrinsics"] else None,
        environment=Environment(row["environment"]) if row["environment"] else None,
        description=row["description"],
        quality=row["quality"],
        usable=bool(row["usable"]),
        reject_reason=row["reject_reason"],
        phash=row["phash"],
    )


def _row_to_view(row: sqlite3.Row) -> View:
    from comfy_studio.atlas.models import CalibState

    return View(
        view_id=row["view_id"],
        place_id=row["place_id"],
        ref_id=row["ref_id"],
        label=row["label"],
        lat=row["lat"],
        lon=row["lon"],
        heading_deg=row["heading_deg"],
        fov_deg=row["fov_deg"],
        calib_state=CalibState(row["calib_state"]),
        method=row["method"],
        usable=bool(row["usable"]),
        notes=row["notes"],
        created_ts=row["created_ts"],
    )


def _row_to_coverage(row: sqlite3.Row) -> Coverage:
    return Coverage(
        place_id=row["place_id"],
        views_observed=row["views_observed"],
        backed=json.loads(row["backed"] or "{}"),
        unobserved=json.loads(row["unobserved"] or "{}"),
        evidence_newest=row["evidence_newest"],
        evidence_oldest=row["evidence_oldest"],
        confidence_basis=row["confidence_basis"],
        updated_ts=row["updated_ts"],
    )


def _row_to_scenario(row: sqlite3.Row) -> Scenario:
    from comfy_studio.atlas.models import ScenarioKind, ScenarioStatus

    return Scenario(
        scenario_id=row["scenario_id"],
        place_id=row["place_id"],
        kind=ScenarioKind(row["kind"]),
        version=row["version"],
        refs=json.loads(row["refs"] or "[]"),
        geometry_path=row["geometry_path"],
        transforms=json.loads(row["transforms"] or "{}"),
        capabilities=json.loads(row["capabilities"] or "{}"),
        status=ScenarioStatus(row["status"]),
        created_ts=row["created_ts"],
    )


def _row_to_production(row: sqlite3.Row) -> ProductionRecord:
    return ProductionRecord(
        prod_id=row["prod_id"],
        atlas_version=row["atlas_version"],
        place_id=row["place_id"],
        view_id=row["view_id"],
        scenario_id=row["scenario_id"],
        camera=json.loads(row["camera"] or "{}"),
        params=json.loads(row["params"] or "{}"),
        result=json.loads(row["result"] or "{}"),
        created_ts=row["created_ts"],
    )


def _row_to_source(row: sqlite3.Row) -> SourceRecord:
    return SourceRecord(
        source_id=row["source_id"],
        name=row["name"],
        kind=row["kind"],
        url=row["url"],
        license=row["license"],
        license_url=row["license_url"],
        terms=row["terms"],
        extract_date=row["extract_date"],
        sha256=row["sha256"],
        fetched_ts=row["fetched_ts"],
        notes=row["notes"],
    )
