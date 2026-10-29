"""Almacén vectorial sqlite-vec (index.db) para embeddings CLIP e InsightFace."""

from __future__ import annotations

import json
import math
import sqlite3
import struct
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# M1.2: el import de `sqlite_vec` es PEREZOSO. Antes era un import duro de módulo
# y una extensión ausente o rota tumbaba TODO el servidor de Studio (proyectos,
# chat, renders), no solo la percepción. Ahora falla con un error tipado que la
# app traduce a `perception_available=False` + HTTP 503 en los endpoints afectados.
SQLITE_VEC_BUSY_TIMEOUT_MS = 15000


class PerceptionUnavailableError(RuntimeError):
    """La búsqueda vectorial no está disponible (falta o falla `sqlite-vec`)."""


def load_sqlite_vec() -> Any:
    """Importa `sqlite_vec` de forma perezosa y traduce cualquier fallo a un error tipado."""
    try:
        import sqlite_vec  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - cualquier fallo de import degrada percepción
        raise PerceptionUnavailableError(
            f"sqlite-vec no está disponible en este entorno: {type(exc).__name__}: {exc}"
        ) from exc
    return sqlite_vec


CLIP_EMBED_DIM = 768
FACE_EMBED_DIM = 512

# Tupla de versión del índice: (clip_dim, face_dim, clip_extractor, face_extractor).
# Se persiste en `schema_meta` y se compara en el arranque para detectar
# desajustes de dimensión (64 -> 768/512 al migrar a backends reales).
SCHEMA_META_KEYS = ("clip_dim", "face_dim", "clip_extractor", "face_extractor", "built_at")

INDEX_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS clip_shots (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    source_asset_id TEXT NOT NULL,
    keyframe_asset_id TEXT NOT NULL UNIQUE,
    timestamp_sec REAL NOT NULL,
    shot_start_sec REAL NOT NULL,
    shot_end_sec REAL NOT NULL,
    caption TEXT NOT NULL DEFAULT '',
    visual_tags_json TEXT NOT NULL DEFAULT '[]',
    embedding_blob BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clip_shots_project ON clip_shots(project_id);
CREATE INDEX IF NOT EXISTS idx_clip_shots_source ON clip_shots(project_id, source_asset_id);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_clip_shots USING vec0(
    embedding float[{CLIP_EMBED_DIM}]
);

CREATE TABLE IF NOT EXISTS face_identities (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    reference_asset_id TEXT,
    embedding_blob BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_face_identities_project ON face_identities(project_id);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_face_identities USING vec0(
    embedding float[{FACE_EMBED_DIM}]
);

CREATE TABLE IF NOT EXISTS face_detections (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    detection_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    source_asset_id TEXT NOT NULL,
    keyframe_asset_id TEXT NOT NULL,
    identity_id TEXT,
    timestamp_sec REAL NOT NULL,
    bbox_json TEXT NOT NULL DEFAULT '[]',
    det_score REAL NOT NULL DEFAULT 1.0,
    embedding_blob BLOB NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{{}}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_face_det_project ON face_detections(project_id);
CREATE INDEX IF NOT EXISTS idx_face_det_keyframe ON face_detections(project_id, keyframe_asset_id);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_face_detections USING vec0(
    embedding float[{FACE_EMBED_DIM}]
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def pack_float32_vector(vec: list[float] | tuple[float, ...], dim: int) -> bytes:
    """Empaqueta y normaliza L2 un vector float32 para sqlite-vec.

    `dim` es obligatorio: se elimina el default 64 para forzar a los
    llamantes a declarar la dimensión del backend activo (768 CLIP / 512 face).
    """
    vals = list(vec[:dim])
    if len(vals) < dim:
        vals.extend([0.0] * (dim - len(vals)))
    norm = math.sqrt(sum(v * v for v in vals))
    if norm > 1e-9:
        vals = [v / norm for v in vals]
    return struct.pack(f"{dim}f", *vals)


def unpack_float32_vector(blob: bytes, dim: int) -> list[float]:
    """Desempaqueta un blob float32 procedente de sqlite-vec.

    `dim` es obligatorio (sin default 64).
    """
    if not blob or len(blob) != dim * 4:
        return [0.0] * dim
    return list(struct.unpack(f"{dim}f", blob))


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Calcula la similitud coseno entre dos vectores."""
    if not vec_a or not vec_b:
        return 0.0
    n = min(len(vec_a), len(vec_b))
    dot = sum(vec_a[i] * vec_b[i] for i in range(n))
    norm_a = math.sqrt(sum(vec_a[i] * vec_a[i] for i in range(n)))
    norm_b = math.sqrt(sum(vec_b[i] * vec_b[i] for i in range(n)))
    if norm_a <= 1e-9 or norm_b <= 1e-9:
        return 0.0
    return float(max(-1.0, min(1.0, dot / (norm_a * norm_b))))


class PerceptionVectorStore:
    """Gestor de `index.db` respaldado por `sqlite-vec` para búsqueda visual y facial."""

    def __init__(self, studio_data_dir: Path | str) -> None:
        raw_path = Path(studio_data_dir).resolve()
        if raw_path.suffix.lower() == ".db":
            self.data_dir = raw_path.parent
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.index_db_path = raw_path
        else:
            self.data_dir = raw_path
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.index_db_path = self.data_dir / "index.db"
        if self.index_db_path.is_dir():
            import shutil

            shutil.rmtree(self.index_db_path, ignore_errors=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        sqlite_vec = load_sqlite_vec()
        conn = sqlite3.connect(self.index_db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        # M1.3: `busy_timeout` uniforme en TODAS las conexiones (antes solo el
        # `timeout=` del constructor) para que las escrituras concurrentes de
        # herramientas del agente esperen en vez de fallar con `database is locked`.
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_VEC_BUSY_TIMEOUT_MS};")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Conexión con cierre garantizado (M3-SCR-009: `with conn` commitía pero nunca cerraba)."""
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(INDEX_SCHEMA_SQL)
            # Registrar meta de versión inicial si el índice está vacío (nuevo).
            existing = conn.execute(
                "SELECT COUNT(*) AS c FROM schema_meta"
            ).fetchone()
            if existing and int(existing["c"]) == 0:
                from datetime import UTC, datetime

                now = datetime.now(UTC).isoformat()
                for key, val in (
                    ("clip_dim", str(CLIP_EMBED_DIM)),
                    ("face_dim", str(FACE_EMBED_DIM)),
                    ("clip_extractor", "openai/clip-vit-large-patch14"),
                    ("face_extractor", "buffalo_l/w600k_r50"),
                    ("built_at", now),
                ):
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                        (key, val),
                    )
                conn.commit()

    def vec_version(self) -> str:
        with self._connection() as conn:
            row = conn.execute("SELECT vec_version() AS v").fetchone()
            return str(row["v"]) if row else ""

    # ------------------------------------------------------------------
    # schema_meta (versión del índice)
    # ------------------------------------------------------------------

    def get_schema_meta(self) -> dict[str, str]:
        """Devuelve todo el contenido de `schema_meta` como dict key→value."""
        with self._connection() as conn:
            rows = conn.execute("SELECT key, value FROM schema_meta").fetchall()
        return {str(r["key"]): str(r["value"]) for r in rows}

    def set_schema_meta(self, key: str, value: str) -> None:
        """Upsert de una entrada de `schema_meta`."""
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            conn.commit()

    def stored_dims(self) -> tuple[int | None, int | None]:
        """Devuelve (clip_dim, face_dim) tal como se guardó en schema_meta,
        o (None, None) si aún no hay meta registrada (índice vacío/nuevo)."""
        meta = self.get_schema_meta()
        clip_dim = None
        face_dim = None
        if "clip_dim" in meta:
            try:
                clip_dim = int(meta["clip_dim"])
            except ValueError:
                clip_dim = None
        if "face_dim" in meta:
            try:
                face_dim = int(meta["face_dim"])
            except ValueError:
                face_dim = None
        return clip_dim, face_dim

    # ------------------------------------------------------------------
    # CLIP Shots (vec_clip_shots + clip_shots)
    # ------------------------------------------------------------------

    def upsert_clip_shot(
        self,
        *,
        project_id: str,
        source_asset_id: str,
        keyframe_asset_id: str,
        timestamp_sec: float,
        shot_start_sec: float,
        shot_end_sec: float,
        embedding: list[float],
        caption: str = "",
        visual_tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blob = pack_float32_vector(embedding, CLIP_EMBED_DIM)
        now = _utc_now_iso()
        tags_json = json.dumps(visual_tags or [], ensure_ascii=False)
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT rowid, shot_id FROM clip_shots WHERE keyframe_asset_id = ?",
                (keyframe_asset_id,),
            ).fetchone()
            if existing is not None:
                rowid = int(existing["rowid"])
                shot_id = str(existing["shot_id"])
                conn.execute("DELETE FROM vec_clip_shots WHERE rowid = ?", (rowid,))
                conn.execute(
                    "UPDATE clip_shots SET project_id = ?, source_asset_id = ?, "
                    "timestamp_sec = ?, shot_start_sec = ?, shot_end_sec = ?, "
                    "caption = ?, visual_tags_json = ?, embedding_blob = ?, "
                    "metadata_json = ?, created_at = ? WHERE rowid = ?",
                    (
                        project_id,
                        source_asset_id,
                        float(timestamp_sec),
                        float(shot_start_sec),
                        float(shot_end_sec),
                        caption,
                        tags_json,
                        blob,
                        meta_json,
                        now,
                        rowid,
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_clip_shots(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
            else:
                shot_id = f"shot-{uuid.uuid4().hex[:12]}"
                cur = conn.execute(
                    "INSERT INTO clip_shots "
                    "(shot_id, project_id, source_asset_id, keyframe_asset_id, "
                    "timestamp_sec, shot_start_sec, shot_end_sec, caption, "
                    "visual_tags_json, embedding_blob, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        shot_id,
                        project_id,
                        source_asset_id,
                        keyframe_asset_id,
                        float(timestamp_sec),
                        float(shot_start_sec),
                        float(shot_end_sec),
                        caption,
                        tags_json,
                        blob,
                        meta_json,
                        now,
                    ),
                )
                rowid = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO vec_clip_shots(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM clip_shots WHERE rowid = ?",
                (rowid,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"clip_shot recién insertado no encontrado (rowid={rowid})")
        return self._row_to_clip_shot(row)

    @staticmethod
    def _row_to_clip_shot(row: sqlite3.Row) -> dict[str, Any]:
        try:
            tags = json.loads(row["visual_tags_json"] or "[]")
        except json.JSONDecodeError:
            tags = []
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return {
            "shot_id": row["shot_id"],
            "project_id": row["project_id"],
            "source_asset_id": row["source_asset_id"],
            "keyframe_asset_id": row["keyframe_asset_id"],
            "timestamp_sec": float(row["timestamp_sec"]),
            "shot_start_sec": float(row["shot_start_sec"]),
            "shot_end_sec": float(row["shot_end_sec"]),
            "caption": row["caption"],
            "visual_tags": tags,
            "metadata": meta,
            "created_at": row["created_at"],
        }

    def list_clip_shots(
        self,
        project_id: str,
        *,
        source_asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if source_asset_id:
            clauses.append("source_asset_id = ?")
            params.append(source_asset_id)
        where_sql = " AND ".join(clauses)
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM clip_shots WHERE {where_sql} ORDER BY timestamp_sec ASC, rowid ASC",
                params,
            ).fetchall()
        return [self._row_to_clip_shot(r) for r in rows]

    def search_clip_shots(
        self,
        project_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 10,
        source_asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query_blob = pack_float32_vector(query_embedding, CLIP_EMBED_DIM)
        query_vec = unpack_float32_vector(query_blob, CLIP_EMBED_DIM)
        with self._connection() as conn:
            total_row = conn.execute("SELECT COUNT(*) AS c FROM vec_clip_shots").fetchone()
            total_count = int(total_row["c"]) if total_row else 0
            if total_count == 0:
                return []
            k_Fetch = max(top_k * 4, min(total_count, 200))
            knn_rows = conn.execute(
                "SELECT rowid, distance FROM vec_clip_shots WHERE embedding MATCH ? AND k = ?",
                (query_blob, k_Fetch),
            ).fetchall()
            distance_by_rowid = {int(r["rowid"]): float(r["distance"]) for r in knn_rows}

            clauses = ["project_id = ?"]
            params: list[Any] = [project_id]
            if source_asset_id:
                clauses.append("source_asset_id = ?")
                params.append(source_asset_id)
            shot_rows = conn.execute(
                f"SELECT * FROM clip_shots WHERE {' AND '.join(clauses)}",
                params,
            ).fetchall()

        results: list[dict[str, Any]] = []
        for row in shot_rows:
            rowid = int(row["rowid"])
            emb = unpack_float32_vector(row["embedding_blob"], CLIP_EMBED_DIM)
            sim = cosine_similarity(query_vec, emb)
            vec_dist = distance_by_rowid.get(rowid, math.sqrt(max(0.0, 2.0 - 2.0 * sim)))
            item = self._row_to_clip_shot(row)
            item["similarity"] = round(sim, 4)
            item["score"] = round(sim, 4)
            item["distance"] = round(vec_dist, 4)
            results.append(item)

        results.sort(key=lambda x: (-x["similarity"], x["distance"], x["timestamp_sec"]))
        return results[: max(1, int(top_k))]

    # ------------------------------------------------------------------
    # Face Identities & Detections (vec_face_identities + vec_face_detections)
    # ------------------------------------------------------------------

    def upsert_face_identity(
        self,
        *,
        identity_id: str,
        project_id: str,
        name: str,
        reference_asset_id: str | None,
        embedding: list[float],
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blob = pack_float32_vector(embedding, FACE_EMBED_DIM)
        now = _utc_now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        with self._connection() as conn:
            existing = conn.execute(
                "SELECT rowid FROM face_identities WHERE identity_id = ?",
                (identity_id,),
            ).fetchone()
            if existing is not None:
                rowid = int(existing["rowid"])
                conn.execute("DELETE FROM vec_face_identities WHERE rowid = ?", (rowid,))
                conn.execute(
                    "UPDATE face_identities SET project_id = ?, name = ?, "
                    "reference_asset_id = ?, embedding_blob = ?, metadata_json = ?, "
                    "created_at = ? WHERE rowid = ?",
                    (
                        project_id,
                        name,
                        reference_asset_id,
                        blob,
                        meta_json,
                        now,
                        rowid,
                    ),
                )
                conn.execute(
                    "INSERT INTO vec_face_identities(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO face_identities "
                    "(identity_id, project_id, name, reference_asset_id, "
                    "embedding_blob, metadata_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        identity_id,
                        project_id,
                        name,
                        reference_asset_id,
                        blob,
                        meta_json,
                        now,
                    ),
                )
                rowid = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO vec_face_identities(rowid, embedding) VALUES (?, ?)",
                    (rowid, blob),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM face_identities WHERE rowid = ?",
                (rowid,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"face_identity recién insertada no encontrada (rowid={rowid})")
        return self._row_to_face_identity(row)

    @staticmethod
    def _row_to_face_identity(row: sqlite3.Row) -> dict[str, Any]:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return {
            "identity_id": row["identity_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "reference_asset_id": row["reference_asset_id"],
            "embedding": unpack_float32_vector(row["embedding_blob"], FACE_EMBED_DIM),
            "metadata": meta,
            "created_at": row["created_at"],
        }

    def get_face_identity(
        self,
        project_id: str,
        identity_id: str,
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM face_identities WHERE project_id = ? AND identity_id = ?",
                (project_id, identity_id),
            ).fetchone()
        return self._row_to_face_identity(row) if row is not None else None

    def get_face_identity_by_name(
        self,
        project_id: str,
        name: str,
    ) -> dict[str, Any] | None:
        target = (name or "").strip().lower()
        if not target:
            return None
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM face_identities WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        for r in rows:
            if str(r["name"] or "").strip().lower() == target:
                return self._row_to_face_identity(r)
        return None

    def clear_keyframe_face_detections(
        self,
        project_id: str,
        keyframe_asset_id: str,
    ) -> None:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT rowid FROM face_detections WHERE project_id = ? AND keyframe_asset_id = ?",
                (project_id, keyframe_asset_id),
            ).fetchall()
            for r in rows:
                conn.execute(
                    "DELETE FROM vec_face_detections WHERE rowid = ?",
                    (int(r["rowid"]),),
                )
            conn.execute(
                "DELETE FROM face_detections WHERE project_id = ? AND keyframe_asset_id = ?",
                (project_id, keyframe_asset_id),
            )
            conn.commit()

    def add_face_detection(
        self,
        *,
        project_id: str,
        source_asset_id: str,
        keyframe_asset_id: str,
        timestamp_sec: float,
        bbox: list[float],
        det_score: float,
        embedding: list[float],
        identity_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        blob = pack_float32_vector(embedding, FACE_EMBED_DIM)
        det_id = f"fdet-{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        with self._connection() as conn:
            cur = conn.execute(
                "INSERT INTO face_detections "
                "(detection_id, project_id, source_asset_id, keyframe_asset_id, "
                "identity_id, timestamp_sec, bbox_json, det_score, embedding_blob, "
                "metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    det_id,
                    project_id,
                    source_asset_id,
                    keyframe_asset_id,
                    identity_id,
                    float(timestamp_sec),
                    json.dumps(bbox),
                    float(det_score),
                    blob,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            rowid = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO vec_face_detections(rowid, embedding) VALUES (?, ?)",
                (rowid, blob),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM face_detections WHERE rowid = ?",
                (rowid,),
            ).fetchone()
        if row is None:
            raise RuntimeError(f"face_detection recién insertada no encontrada (rowid={rowid})")
        return self._row_to_face_detection(row)

    @staticmethod
    def _row_to_face_detection(row: sqlite3.Row) -> dict[str, Any]:
        try:
            bbox = json.loads(row["bbox_json"] or "[]")
        except json.JSONDecodeError:
            bbox = []
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return {
            "detection_id": row["detection_id"],
            "project_id": row["project_id"],
            "source_asset_id": row["source_asset_id"],
            "keyframe_asset_id": row["keyframe_asset_id"],
            "identity_id": row["identity_id"],
            "timestamp_sec": float(row["timestamp_sec"]),
            "bbox": bbox,
            "det_score": float(row["det_score"]),
            "embedding": unpack_float32_vector(row["embedding_blob"], FACE_EMBED_DIM),
            "metadata": meta,
            "created_at": row["created_at"],
        }

    def search_face_detections(
        self,
        project_id: str,
        query_embedding: list[float],
        *,
        top_k: int = 20,
        min_similarity: float = 0.55,
    ) -> list[dict[str, Any]]:
        query_blob = pack_float32_vector(query_embedding, FACE_EMBED_DIM)
        query_vec = unpack_float32_vector(query_blob, FACE_EMBED_DIM)
        with self._connection() as conn:
            total_row = conn.execute("SELECT COUNT(*) AS c FROM vec_face_detections").fetchone()
            total_count = int(total_row["c"]) if total_row else 0
            if total_count == 0:
                return []
            k_fetch = max(top_k * 4, min(total_count, 200))
            knn_rows = conn.execute(
                "SELECT rowid, distance FROM vec_face_detections WHERE embedding MATCH ? AND k = ?",
                (query_blob, k_fetch),
            ).fetchall()
            distance_by_rowid = {int(r["rowid"]): float(r["distance"]) for r in knn_rows}
            det_rows = conn.execute(
                "SELECT * FROM face_detections WHERE project_id = ?",
                (project_id,),
            ).fetchall()

        matches: list[dict[str, Any]] = []
        for row in det_rows:
            rowid = int(row["rowid"])
            emb = unpack_float32_vector(row["embedding_blob"], FACE_EMBED_DIM)
            sim = cosine_similarity(query_vec, emb)
            if sim < min_similarity:
                continue
            vec_dist = distance_by_rowid.get(rowid, math.sqrt(max(0.0, 2.0 - 2.0 * sim)))
            item = self._row_to_face_detection(row)
            item.pop("embedding", None)
            item["similarity"] = round(sim, 4)
            item["score"] = round(sim, 4)
            item["distance"] = round(vec_dist, 4)
            matches.append(item)

        matches.sort(key=lambda x: (-x["similarity"], x["distance"], x["timestamp_sec"]))
        return matches[: max(1, int(top_k))]
