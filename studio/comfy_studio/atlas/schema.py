"""Esquema SQLite del catálogo del atlas (RTree espacial + FTS5 textual).

El índice vive en disco con el atlas: no hay servidor vectorial ni servicio
externo. RTree indexa bounds en grados (WGS84) para búsqueda espacial; FTS5
indexa nombres/alias y títulos. La consulta básica funciona sin descargar
ningún modelo.
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT NOT NULL UNIQUE,
    municipality TEXT NOT NULL,
    name TEXT NOT NULL,
    name_norm TEXT NOT NULL,
    kind TEXT NOT NULL,
    aliases TEXT NOT NULL DEFAULT '[]',
    sector TEXT,
    lat REAL,
    lon REAL,
    min_lat REAL,
    max_lat REAL,
    min_lon REAL,
    max_lon REAL,
    osm_refs TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT '{}',
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_places_municipality ON places(municipality);
CREATE INDEX IF NOT EXISTS idx_places_kind ON places(kind);
CREATE INDEX IF NOT EXISTS idx_places_name_norm ON places(name_norm);
CREATE VIRTUAL TABLE IF NOT EXISTS places_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);
CREATE VIRTUAL TABLE IF NOT EXISTS places_fts USING fts5(
    name, aliases, municipality
);

CREATE TABLE IF NOT EXISTS atlas_refs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ref_id TEXT NOT NULL UNIQUE,
    place_id TEXT REFERENCES places(place_id),
    title TEXT NOT NULL,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    source_page TEXT,
    source_file_url TEXT,
    sha1_source TEXT,
    author TEXT,
    license TEXT NOT NULL,
    license_url TEXT,
    attribution TEXT,
    capture_date TEXT,
    upload_date TEXT,
    date_reliability TEXT NOT NULL,
    fetched_ts REAL NOT NULL,
    width INTEGER,
    height INTEGER,
    native_width INTEGER,
    native_height INTEGER,
    bytes INTEGER,
    mime TEXT,
    resolution_class TEXT NOT NULL DEFAULT 'original',
    derived_from_url TEXT,
    gps_lat REAL,
    gps_lon REAL,
    heading_deg REAL,
    intrinsics TEXT,
    environment TEXT,
    description TEXT,
    quality REAL NOT NULL DEFAULT 0.0,
    usable INTEGER NOT NULL DEFAULT 1,
    reject_reason TEXT,
    phash TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_place ON atlas_refs(place_id);
CREATE INDEX IF NOT EXISTS idx_refs_usable ON atlas_refs(usable);
CREATE INDEX IF NOT EXISTS idx_refs_license ON atlas_refs(license);
CREATE INDEX IF NOT EXISTS idx_refs_sha1 ON atlas_refs(sha1_source);
CREATE VIRTUAL TABLE IF NOT EXISTS refs_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);
CREATE VIRTUAL TABLE IF NOT EXISTS refs_fts USING fts5(
    title, author, description
);

CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    view_id TEXT NOT NULL UNIQUE,
    place_id TEXT NOT NULL REFERENCES places(place_id),
    ref_id TEXT NOT NULL REFERENCES atlas_refs(ref_id),
    label TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    heading_deg REAL,
    fov_deg REAL,
    calib_state TEXT NOT NULL DEFAULT 'uncalibrated',
    method TEXT NOT NULL DEFAULT 'gps_metadata',
    usable INTEGER NOT NULL DEFAULT 1,
    notes TEXT,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_views_place ON views(place_id);
CREATE INDEX IF NOT EXISTS idx_views_ref ON views(ref_id);

CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id TEXT NOT NULL UNIQUE,
    place_id TEXT NOT NULL REFERENCES places(place_id),
    kind TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    refs TEXT NOT NULL DEFAULT '[]',
    geometry_path TEXT,
    transforms TEXT NOT NULL DEFAULT '{}',
    capabilities TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scenarios_place ON scenarios(place_id);

CREATE TABLE IF NOT EXISTS coverage (
    place_id TEXT PRIMARY KEY REFERENCES places(place_id),
    views_observed INTEGER NOT NULL DEFAULT 0,
    backed TEXT NOT NULL DEFAULT '{}',
    unobserved TEXT NOT NULL DEFAULT '{}',
    evidence_newest TEXT,
    evidence_oldest TEXT,
    confidence_basis TEXT NOT NULL DEFAULT '',
    updated_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS productions (
    prod_id TEXT PRIMARY KEY,
    atlas_version TEXT NOT NULL,
    place_id TEXT,
    view_id TEXT,
    scenario_id TEXT,
    camera TEXT NOT NULL DEFAULT '{}',
    params TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL DEFAULT '{}',
    created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    license TEXT NOT NULL,
    license_url TEXT,
    terms TEXT,
    extract_date TEXT,
    sha256 TEXT,
    fetched_ts REAL NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS licenses (
    ref_id TEXT PRIMARY KEY REFERENCES atlas_refs(ref_id),
    license TEXT NOT NULL,
    license_url TEXT,
    attribution TEXT,
    license_text_path TEXT
);

-- Acquisition reanudable: cada item por (run, source, key) queda marcado.
CREATE TABLE IF NOT EXISTS acq_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    started_ts REAL NOT NULL,
    finished_ts REAL,
    status TEXT NOT NULL DEFAULT 'running',
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT
);
CREATE TABLE IF NOT EXISTS acq_items (
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    ts REAL NOT NULL,
    PRIMARY KEY (run_id, source, key)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Crea/migra el esquema; fija meta.schema_version."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_DDL)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row[0]) > SCHEMA_VERSION:
        raise RuntimeError(
            f"catalog.db es de esquema {row[0]} > {SCHEMA_VERSION}; actualizar el código"
        )
    else:
        # migración incremental (v1 -> v2: dimensiones nativas de la fuente)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(atlas_refs)").fetchall()}
        if "native_width" not in cols:
            conn.execute("ALTER TABLE atlas_refs ADD COLUMN native_width INTEGER")
        if "native_height" not in cols:
            conn.execute("ALTER TABLE atlas_refs ADD COLUMN native_height INTEGER")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
        )
    conn.commit()
