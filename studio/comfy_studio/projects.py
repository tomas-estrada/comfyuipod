"""Persistencia SQLite (studio.db) y gestión de proyectos, jobs y medios."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

PROJECT_SUBDIRS: tuple[str, ...] = ("uploads", "outputs", "keyframes", "voice")

# Mapa MIME permitido -> (kind, conjunto de extensiones permitidas)
ALLOWED_MEDIA_MIMES: dict[str, tuple[str, set[str]]] = {
    # Vídeo
    "video/mp4": ("video", {".mp4", ".m4v"}),
    "video/webm": ("video", {".webm"}),
    "video/quicktime": ("video", {".mov", ".qt"}),
    "video/x-matroska": ("video", {".mkv"}),
    # Foto / Imagen
    "image/png": ("image", {".png"}),
    "image/jpeg": ("image", {".jpg", ".jpeg"}),
    "image/webp": ("image", {".webp"}),
    # Audio
    "audio/wav": ("audio", {".wav", ".wave"}),
    "audio/x-wav": ("audio", {".wav", ".wave"}),
    "audio/wave": ("audio", {".wav", ".wave"}),
    "audio/mpeg": ("audio", {".mp3", ".mpeg"}),
    "audio/mp3": ("audio", {".mp3"}),
    "audio/ogg": ("audio", {".ogg", ".oga"}),
    "audio/flac": ("audio", {".flac"}),
    "audio/mp4": ("audio", {".m4a", ".mp4"}),
    "audio/x-m4a": ("audio", {".m4a"}),
}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class MediaValidationError(Exception):
    """Error tipado de validación de subida de medios con código HTTP asociado."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sanitize_filename(raw_name: str | None, fallback_ext: str = ".bin") -> str:
    """Extrae el nombre base eliminando componentes de ruta y caracteres inseguros."""
    if not raw_name:
        return f"media_{uuid.uuid4().hex[:8]}{fallback_ext}"
    # Soportar separadores POSIX y Windows
    normalized = raw_name.replace("\\", "/").split("/")[-1].strip()
    if not normalized or normalized in {".", ".."}:
        return f"media_{uuid.uuid4().hex[:8]}{fallback_ext}"
    stem_and_ext = Path(normalized)
    ext = stem_and_ext.suffix.lower()
    stem = _SAFE_FILENAME_RE.sub("_", stem_and_ext.stem).strip("._-")
    if not stem:
        stem = f"media_{uuid.uuid4().hex[:8]}"
    ext_clean = _SAFE_FILENAME_RE.sub("", ext)
    if ext_clean and not ext_clean.startswith("."):
        ext_clean = f".{ext_clean}"
    return f"{stem[:80]}{ext_clean or fallback_ext}"


def validate_media_type(
    filename: str | None,
    content_type: str | None,
) -> tuple[str, str, str]:
    """Valida MIME y extensión. Devuelve (kind, normalized_mime, sanitized_filename)."""
    raw_mime = (content_type or "").split(";", 1)[0].strip().lower()
    if raw_mime not in ALLOWED_MEDIA_MIMES:
        raise MediaValidationError(
            f"Tipo MIME no soportado: '{raw_mime or 'desconocido'}'. "
            "Solo se admiten formatos de vídeo, imagen o audio autorizados.",
            status_code=415,
        )
    kind, allowed_exts = ALLOWED_MEDIA_MIMES[raw_mime]
    default_ext = sorted(allowed_exts)[0]
    safe_name = sanitize_filename(filename, fallback_ext=default_ext)
    ext = Path(safe_name).suffix.lower()
    if ext not in allowed_exts:
        raise MediaValidationError(
            f"La extensión '{ext}' no coincide con el tipo MIME '{raw_mime}'.",
            status_code=415,
        )
    return kind, raw_mime, safe_name


SCHEMA_V1_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    aspect_ratio TEXT NOT NULL DEFAULT '16:9',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    template_id TEXT,
    prompt_id TEXT,
    progress_value INTEGER NOT NULL DEFAULT 0,
    progress_max INTEGER NOT NULL DEFAULT 100,
    current_node TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'uploads',
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transcriptions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    asset_id TEXT REFERENCES assets(id) ON DELETE CASCADE,
    language TEXT NOT NULL DEFAULT 'es',
    segments_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS identities (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    reference_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edl_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL DEFAULT 1,
    edl_json TEXT NOT NULL DEFAULT '{}',
    output_asset_id TEXT REFERENCES assets(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_project_id ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_project_id ON chat_messages(project_id);
"""


class ProjectStore:
    """Almacén SQLite (studio.db) y gestor de archivos bajo STUDIO_DATA_DIR/projects."""

    def __init__(self, studio_data_dir: Path | str) -> None:
        self.data_dir = Path(studio_data_dir).resolve()
        self.projects_root = (self.data_dir / "projects").resolve()
        self.db_path = self.data_dir / "studio.db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript(SCHEMA_V1_SQL)
            existing = conn.execute(
                "SELECT version FROM schema_version WHERE version = ?",
                (SCHEMA_VERSION,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (
                        SCHEMA_VERSION,
                        _utc_now_iso(),
                        "Initial schema: projects, jobs, assets, chat, transcriptions, edl",
                    ),
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
            conn.commit()

    def get_schema_version(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            return int(row["v"]) if row and row["v"] is not None else 0

    def _validate_project_id(self, project_id: str) -> str:
        cleaned = (project_id or "").strip()
        if not _SAFE_ID_RE.match(cleaned):
            raise ValueError("Identificador de proyecto inválido")
        return cleaned

    def project_dir(self, project_id: str) -> Path:
        safe_id = self._validate_project_id(project_id)
        target = (self.projects_root / safe_id).resolve()
        if not target.is_relative_to(self.projects_root):
            raise ValueError("Ruta de proyecto fuera de STUDIO_DATA_DIR/projects")
        return target

    def _ensure_project_dirs(self, project_id: str) -> Path:
        pdir = self.project_dir(project_id)
        pdir.mkdir(parents=True, exist_ok=True)
        for sub in PROJECT_SUBDIRS:
            (pdir / sub).mkdir(parents=True, exist_ok=True)
        return pdir

    @staticmethod
    def _row_to_project(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "aspect_ratio": row["aspect_ratio"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "category": row["category"],
            "filename": row["filename"],
            "original_filename": row["original_filename"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "rel_path": row["rel_path"],
            "version": row["version"],
            "job_id": row["job_id"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "status": row["status"],
            "template_id": row["template_id"],
            "prompt_id": row["prompt_id"],
            "progress_value": row["progress_value"],
            "progress_max": row["progress_max"],
            "current_node": row["current_node"],
            "params": json.loads(row["params_json"] or "{}"),
            "result": json.loads(row["result_json"] or "{}"),
            "error": row["error"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # --- CRUD Proyectos ---

    def create_project(
        self,
        name: str,
        description: str = "",
        aspect_ratio: str = "16:9",
        metadata: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("El nombre del proyecto no puede estar vacío")
        pid = self._validate_project_id(project_id) if project_id else uuid.uuid4().hex[:12]
        now = _utc_now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        self._ensure_project_dirs(pid)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects "
                "(id, name, description, aspect_ratio, metadata_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    cleaned_name,
                    description.strip(),
                    aspect_ratio.strip() or "16:9",
                    meta_json,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        return self._row_to_project(row)

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        try:
            safe_id = self._validate_project_id(project_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (safe_id,)
            ).fetchone()
            if row is None:
                return None
            proj = self._row_to_project(row)
            asset_rows = conn.execute(
                "SELECT * FROM assets WHERE project_id = ? ORDER BY created_at ASC",
                (safe_id,),
            ).fetchall()
            proj["assets"] = [self._row_to_asset(a) for a in asset_rows]
            return proj

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        aspect_ratio: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_project(project_id)
        if current is None:
            return None
        new_name = name.strip() if name is not None else current["name"]
        if not new_name:
            raise ValueError("El nombre del proyecto no puede estar vacío")
        new_desc = description.strip() if description is not None else current["description"]
        new_ar = aspect_ratio.strip() if aspect_ratio is not None else current["aspect_ratio"]
        new_meta = metadata if metadata is not None else current["metadata"]
        now = _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                "UPDATE projects SET name = ?, description = ?, aspect_ratio = ?, "
                "metadata_json = ?, updated_at = ? WHERE id = ?",
                (
                    new_name,
                    new_desc,
                    new_ar,
                    json.dumps(new_meta, ensure_ascii=False),
                    now,
                    current["id"],
                ),
            )
            conn.commit()
        return self.get_project(current["id"])

    def delete_project(self, project_id: str) -> bool:
        try:
            safe_id = self._validate_project_id(project_id)
        except ValueError:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM projects WHERE id = ?", (safe_id,)
            ).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM projects WHERE id = ?", (safe_id,))
            conn.commit()

        pdir = self.project_dir(safe_id)
        if pdir.exists() and pdir.is_relative_to(self.projects_root):
            shutil.rmtree(pdir, ignore_errors=True)
        return True

    # --- Subida y gestión de medios (assets) ---

    def save_upload(
        self,
        project_id: str,
        *,
        filename: str | None,
        content_type: str | None,
        data: bytes,
        max_upload_bytes: int,
        category: str = "uploads",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.get_project(project_id) is None:
            raise LookupError(f"Proyecto '{project_id}' no encontrado")

        if category not in PROJECT_SUBDIRS:
            raise MediaValidationError(
                f"Categoría de medio no válida: '{category}'", status_code=400
            )

        kind, normalized_mime, safe_name = validate_media_type(filename, content_type)
        size_bytes = len(data)
        if size_bytes == 0:
            raise MediaValidationError(
                "El archivo subido está vacío (0 bytes).", status_code=400
            )
        if size_bytes > max_upload_bytes:
            raise MediaValidationError(
                f"El archivo ({size_bytes} bytes) supera el límite máximo permitido "
                f"({max_upload_bytes} bytes).",
                status_code=413,
            )

        asset_id = uuid.uuid4().hex[:12]
        stored_filename = f"{asset_id}_{safe_name}"
        pdir = self._ensure_project_dirs(project_id)
        category_dir = (pdir / category).resolve()
        target_path = (category_dir / stored_filename).resolve()
        if not target_path.is_relative_to(category_dir):
            raise MediaValidationError(
                "Nombre de archivo rechazado por política de ruta.", status_code=400
            )

        target_path.write_bytes(data)
        rel_path = target_path.relative_to(self.data_dir).as_posix()
        now = _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                "INSERT INTO assets "
                "(id, project_id, kind, category, filename, original_filename, "
                "mime_type, size_bytes, rel_path, version, job_id, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    asset_id,
                    project_id,
                    kind,
                    category,
                    stored_filename,
                    safe_name,
                    normalized_mime,
                    size_bytes,
                    rel_path,
                    1,
                    None,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (now, project_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
        return self._row_to_asset(row)

    def list_assets(
        self,
        project_id: str,
        *,
        kind: str | None = None,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        safe_id = self._validate_project_id(project_id)
        clauses = ["project_id = ?"]
        params: list[Any] = [safe_id]
        if kind:
            clauses.append("kind = ?")
            params.append(kind.strip())
        if category:
            clauses.append("category = ?")
            params.append(category.strip())
        where_sql = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM assets WHERE {where_sql} ORDER BY created_at ASC, id ASC",
                params,
            ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    def get_asset(self, project_id: str, asset_id: str) -> dict[str, Any] | None:
        try:
            safe_pid = self._validate_project_id(project_id)
            safe_aid = self._validate_project_id(asset_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE id = ? AND project_id = ?",
                (safe_aid, safe_pid),
            ).fetchone()
        return self._row_to_asset(row) if row is not None else None

    def get_asset_path(self, project_id: str, asset_id: str) -> Path | None:
        asset = self.get_asset(project_id, asset_id)
        if asset is None:
            return None
        pdir = self.project_dir(project_id)
        full_path = (self.data_dir / asset["rel_path"]).resolve()
        if not full_path.is_relative_to(pdir) or not full_path.is_file():
            return None
        return full_path

    def delete_asset(self, project_id: str, asset_id: str) -> bool:
        asset = self.get_asset(project_id, asset_id)
        if asset is None:
            return False
        pdir = self.project_dir(project_id)
        full_path = (self.data_dir / asset["rel_path"]).resolve()
        if full_path.is_relative_to(pdir) and full_path.exists():
            full_path.unlink(missing_ok=True)
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM assets WHERE id = ? AND project_id = ?",
                (asset["id"], project_id),
            )
            conn.commit()
        return True

    # --- CRUD Jobs ---

    def create_job(
        self,
        *,
        kind: str,
        project_id: str | None = None,
        template_id: str | None = None,
        prompt_id: str | None = None,
        params: dict[str, Any] | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex[:12]
        now = _utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO jobs "
                "(id, project_id, kind, status, template_id, prompt_id, "
                "progress_value, progress_max, current_node, params_json, "
                "result_json, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    project_id,
                    kind.strip() or "render",
                    status,
                    template_id,
                    prompt_id,
                    0,
                    100,
                    None,
                    json.dumps(params or {}, ensure_ascii=False),
                    "{}",
                    None,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            safe_jid = self._validate_project_id(job_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (safe_jid,)
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        project_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        prompt_id: str | None = None,
        progress_value: int | None = None,
        progress_max: int | None = None,
        current_node: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_job(job_id)
        if current is None:
            return None
        new_status = status if status is not None else current["status"]
        new_prompt_id = prompt_id if prompt_id is not None else current["prompt_id"]
        new_val = progress_value if progress_value is not None else current["progress_value"]
        new_max = progress_max if progress_max is not None else current["progress_max"]
        new_node = current_node if current_node is not None else current["current_node"]
        new_result = result if result is not None else current["result"]
        new_error = error if error is not None else current["error"]
        now = _utc_now_iso()

        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, prompt_id = ?, progress_value = ?, "
                "progress_max = ?, current_node = ?, result_json = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    new_status,
                    new_prompt_id,
                    new_val,
                    new_max,
                    new_node,
                    json.dumps(new_result, ensure_ascii=False),
                    new_error,
                    now,
                    current["id"],
                ),
            )
            conn.commit()
        return self.get_job(current["id"])

    # ------------------------------------------------------------------
    # Chat messages (session memory per project)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_chat_message(row: sqlite3.Row) -> dict[str, Any]:
        raw_tc = row["tool_calls_json"]
        try:
            tc_data = json.loads(raw_tc) if raw_tc else []
        except json.JSONDecodeError:
            tc_data = []
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "role": row["role"],
            "content": row["content"],
            "tool_calls": tc_data,
            "created_at": row["created_at"],
        }

    def add_chat_message(
        self,
        project_id: str,
        *,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")
        pid = project["id"]
        msg_id = f"msg-{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        tc_json = json.dumps(tool_calls or [], ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO chat_messages "
                "(id, project_id, role, content, tool_calls_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (msg_id, pid, role, content, tc_json, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM chat_messages WHERE id = ?",
                (msg_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_chat_message(row)

    def list_chat_messages(
        self,
        project_id: str,
        *,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE project_id = ? "
                "ORDER BY created_at ASC, rowid ASC LIMIT ?",
                (pid, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_chat_message(r) for r in rows]

    def clear_chat_messages(self, project_id: str) -> int:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM chat_messages WHERE project_id = ?",
                (pid,),
            )
            conn.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # EDL versions (propose_edl / build_edl)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_edl_version(row: sqlite3.Row) -> dict[str, Any]:
        try:
            edl_doc = json.loads(row["edl_json"] or "{}")
        except json.JSONDecodeError:
            edl_doc = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "version": int(row["version"]),
            "edl": edl_doc,
            "output_asset_id": row["output_asset_id"],
            "created_at": row["created_at"],
        }

    def save_edl_version(
        self,
        project_id: str,
        *,
        edl: dict[str, Any],
        output_asset_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")
        pid = project["id"]
        edl_id = f"edl-{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        with self._connect() as conn:
            row_ver = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_ver "
                "FROM edl_versions WHERE project_id = ?",
                (pid,),
            ).fetchone()
            next_version = int(row_ver["max_ver"] if row_ver else 0) + 1
            edl_payload = dict(edl)
            edl_payload["version"] = next_version
            conn.execute(
                "INSERT INTO edl_versions "
                "(id, project_id, version, edl_json, output_asset_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    edl_id,
                    pid,
                    next_version,
                    json.dumps(edl_payload, ensure_ascii=False),
                    output_asset_id,
                    now,
                ),
            )
            conn.commit()
            saved_row = conn.execute(
                "SELECT * FROM edl_versions WHERE id = ?",
                (edl_id,),
            ).fetchone()
        assert saved_row is not None
        return self._row_to_edl_version(saved_row)

    def list_edl_versions(self, project_id: str) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM edl_versions WHERE project_id = ? "
                "ORDER BY version DESC, created_at DESC",
                (pid,),
            ).fetchall()
        return [self._row_to_edl_version(r) for r in rows]

    def get_latest_edl(self, project_id: str) -> dict[str, Any] | None:
        versions = self.list_edl_versions(project_id)
        return versions[0] if versions else None

