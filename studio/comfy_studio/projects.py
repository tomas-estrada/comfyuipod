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
    "image/gif": ("image", {".gif"}),
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

CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT '',
    steps_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_meta (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);

CREATE INDEX IF NOT EXISTS idx_assets_project_id ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_project_id ON chat_messages(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_project_id ON plans(project_id);
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
        aid = row["id"]
        pid = row["project_id"]
        return {
            "id": aid,
            "project_id": pid,
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
            "preview_url": f"/studio/api/projects/{pid}/assets/{aid}/file",
            "download_url": f"/studio/api/projects/{pid}/assets/{aid}/file?download=1",
            "view_url": f"/studio/api/view?project_id={pid}&asset_id={aid}",
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
        job_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self.get_project(project_id) is None:
            raise LookupError(f"Proyecto '{project_id}' no encontrado")
        # `get_project` valida tras `strip()`; usar el id normalizado en INSERT/rutas evita
        # violaciones de FK (500) con ids con espacios alrededor.
        project_id = self._validate_project_id(project_id)

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
                    job_id,
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

    def update_asset_metadata(
        self,
        project_id: str,
        asset_id: str,
        *,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Fusiona etiquetas y metadatos en el asset (M3 tool-belt `tag_asset`)."""
        current = self.get_asset(project_id, asset_id)
        if current is None:
            return None
        merged = dict(current.get("metadata") or {})
        if metadata:
            for key, value in metadata.items():
                merged[str(key)] = value
        if tags is not None:
            cleaned = [str(t).strip() for t in tags if str(t).strip()]
            existing = [str(t) for t in (merged.get("tags") or []) if str(t).strip()]
            merged["tags"] = list(dict.fromkeys([*existing, *cleaned]))
        with self._connect() as conn:
            conn.execute(
                "UPDATE assets SET metadata_json = ? WHERE id = ? AND project_id = ?",
                (
                    json.dumps(merged, ensure_ascii=False),
                    current["id"],
                    current["project_id"],
                ),
            )
            conn.commit()
        return self.get_asset(project_id, asset_id)

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
        # `cancelled` es terminal y "pegajoso": eventos tardíos del WS/poll de ComfyUI
        # (progreso, done, error tras /interrupt) no deben resucitar ni sobrescribir
        # un job que el usuario/agente ya canceló.
        if current["status"] == "cancelled" and status != "cancelled":
            return current
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
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (pid, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_chat_message(r) for r in reversed(rows)]

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

    def get_edl_version(
        self,
        project_id: str,
        edl_id_or_version: str | int | None = None,
        *,
        edl_id: str | None = None,
        version: int | None = None,
    ) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        target = edl_id_or_version
        if target is None:
            if edl_id is not None:
                target = edl_id
            elif version is not None:
                target = version
            else:
                return self.get_latest_edl(pid)
        with self._connect() as conn:
            if isinstance(target, int) or (
                isinstance(target, str) and target.isdigit()
            ):
                row = conn.execute(
                    "SELECT * FROM edl_versions WHERE project_id = ? AND version = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (pid, int(target)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM edl_versions WHERE project_id = ? AND id = ?",
                    (pid, str(target).strip()),
                ).fetchone()
        return self._row_to_edl_version(row) if row is not None else None

    def update_edl_output(
        self,
        project_id: str,
        edl_id: str,
        output_asset_id: str | None = None,
        *,
        edl_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_edl_version(project_id, edl_id)
        if current is None:
            return None
        edl_doc = dict(current["edl"])
        if edl_updates:
            edl_doc.update(edl_updates)
        eff_output_id = (
            output_asset_id if output_asset_id is not None else current["output_asset_id"]
        )
        with self._connect() as conn:
            conn.execute(
                "UPDATE edl_versions SET edl_json = ?, output_asset_id = ? "
                "WHERE id = ? AND project_id = ?",
                (
                    json.dumps(edl_doc, ensure_ascii=False),
                    eff_output_id,
                    current["id"],
                    current["project_id"],
                ),
            )
            conn.commit()
        return self.get_edl_version(project_id, current["id"])

    # ------------------------------------------------------------------
    # Transcriptions (ASR whisperx + diarization FunASR cam++)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_transcription(row: sqlite3.Row) -> dict[str, Any]:
        raw = row["segments_json"] or "[]"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []

        if isinstance(parsed, dict):
            segments = list(parsed.get("segments") or [])
            words = list(parsed.get("words") or [])
            speakers = list(parsed.get("speakers") or [])
            speaker_ranges = list(parsed.get("speaker_ranges") or [])
            by_speaker = dict(parsed.get("by_speaker") or {})
            diarized = bool(parsed.get("diarized", False))
            backend = str(parsed.get("backend") or "whisperx")
            diarization_backend = parsed.get("diarization_backend")
            compute_config = dict(parsed.get("compute_config") or {})
            full_text = str(parsed.get("text") or "").strip()
        elif isinstance(parsed, list):
            segments = parsed
            words = []
            for seg in segments:
                if isinstance(seg, dict) and isinstance(seg.get("words"), list):
                    words.extend(seg["words"])
            speakers = sorted(
                {
                    str(seg.get("speaker"))
                    for seg in segments
                    if isinstance(seg, dict) and seg.get("speaker")
                }
            )
            speaker_ranges = []
            by_speaker = {}
            diarized = bool(speakers)
            backend = "whisperx"
            diarization_backend = None
            compute_config = {}
            full_text = " ".join(
                str(seg.get("text") or "").strip()
                for seg in segments
                if isinstance(seg, dict) and str(seg.get("text") or "").strip()
            )
        else:
            segments = []
            words = []
            speakers = []
            speaker_ranges = []
            by_speaker = {}
            diarized = False
            backend = "whisperx"
            diarization_backend = None
            compute_config = {}
            full_text = ""

        if not full_text and segments:
            full_text = " ".join(
                str(seg.get("text") or "").strip()
                for seg in segments
                if isinstance(seg, dict) and str(seg.get("text") or "").strip()
            )

        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "asset_id": row["asset_id"],
            "language": row["language"],
            "text": full_text,
            "segments": segments,
            "words": words,
            "speakers": speakers,
            "speaker_ranges": speaker_ranges,
            "by_speaker": by_speaker,
            "diarized": diarized,
            "backend": backend,
            "diarization_backend": diarization_backend,
            "compute_config": compute_config,
            "created_at": row["created_at"],
        }

    def save_transcription(
        self,
        project_id: str,
        *,
        asset_id: str | None = None,
        language: str = "es",
        segments: list[dict[str, Any]],
        words: list[dict[str, Any]] | None = None,
        speakers: list[str] | None = None,
        speaker_ranges: list[dict[str, Any]] | None = None,
        by_speaker: dict[str, Any] | None = None,
        diarized: bool = False,
        backend: str = "whisperx",
        diarization_backend: str | None = None,
        compute_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")
        pid = project["id"]
        if asset_id is not None and self.get_asset(pid, asset_id) is None:
            raise ValueError(f"Asset no encontrado en proyecto: {asset_id!r}")

        tr_id = f"tr-{uuid.uuid4().hex[:12]}"
        now = _utc_now_iso()
        resolved_words = (
            list(words)
            if words is not None
            else [
                w
                for seg in segments
                if isinstance(seg, dict)
                for w in (seg.get("words") or [])
                if isinstance(w, dict)
            ]
        )
        resolved_speakers = (
            list(speakers)
            if speakers is not None
            else sorted(
                {
                    str(seg.get("speaker"))
                    for seg in segments
                    if isinstance(seg, dict) and seg.get("speaker")
                }
            )
        )
        full_text = " ".join(
            str(seg.get("text") or "").strip()
            for seg in segments
            if isinstance(seg, dict) and str(seg.get("text") or "").strip()
        )
        envelope = {
            "text": full_text,
            "segments": segments,
            "words": resolved_words,
            "speakers": resolved_speakers,
            "speaker_ranges": list(speaker_ranges or []),
            "by_speaker": dict(by_speaker or {}),
            "diarized": bool(diarized or resolved_speakers),
            "backend": backend,
            "diarization_backend": diarization_backend,
            "compute_config": dict(compute_config or {}),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO transcriptions "
                "(id, project_id, asset_id, language, segments_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    tr_id,
                    pid,
                    asset_id,
                    (language or "es").strip() or "es",
                    json.dumps(envelope, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ?",
                (tr_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_transcription(row)

    def get_transcription(
        self,
        project_id: str,
        transcription_id: str,
    ) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM transcriptions WHERE id = ? AND project_id = ?",
                (str(transcription_id).strip(), pid),
            ).fetchone()
        return self._row_to_transcription(row) if row is not None else None

    def list_transcriptions(
        self,
        project_id: str,
        *,
        asset_id: str | None = None,
    ) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        clauses = ["project_id = ?"]
        params: list[Any] = [pid]
        if asset_id:
            clauses.append("asset_id = ?")
            params.append(asset_id.strip())
        where_sql = " AND ".join(clauses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM transcriptions WHERE {where_sql} "
                "ORDER BY created_at DESC, rowid DESC",
                params,
            ).fetchall()
        return [self._row_to_transcription(r) for r in rows]

    def get_latest_transcription(
        self,
        project_id: str,
        *,
        asset_id: str | None = None,
    ) -> dict[str, Any] | None:
        items = self.list_transcriptions(project_id, asset_id=asset_id)
        return items[0] if items else None

    def update_transcription(
        self,
        project_id: str,
        transcription_id: str,
        *,
        segments: list[dict[str, Any]],
        words: list[dict[str, Any]] | None = None,
        speakers: list[str] | None = None,
        speaker_ranges: list[dict[str, Any]] | None = None,
        by_speaker: dict[str, Any] | None = None,
        diarized: bool = True,
        diarization_backend: str | None = "funasr_campplus",
        compute_config: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_transcription(project_id, transcription_id)
        if current is None:
            return None
        resolved_words = (
            list(words)
            if words is not None
            else [
                w
                for seg in segments
                if isinstance(seg, dict)
                for w in (seg.get("words") or [])
                if isinstance(w, dict)
            ]
        )
        resolved_speakers = (
            list(speakers)
            if speakers is not None
            else sorted(
                {
                    str(seg.get("speaker"))
                    for seg in segments
                    if isinstance(seg, dict) and seg.get("speaker")
                }
            )
        )
        full_text = " ".join(
            str(seg.get("text") or "").strip()
            for seg in segments
            if isinstance(seg, dict) and str(seg.get("text") or "").strip()
        )
        eff_compute_cfg = dict(current.get("compute_config") or {})
        if compute_config:
            eff_compute_cfg.update(compute_config)
        diar_meta = (
            eff_compute_cfg.get("diarization")
            if isinstance(eff_compute_cfg.get("diarization"), dict)
            else {}
        )
        eff_by_speaker = dict(
            by_speaker or diar_meta.get("by_speaker") or current.get("by_speaker") or {}
        )
        eff_speaker_ranges = list(
            speaker_ranges or diar_meta.get("turns") or current.get("speaker_ranges") or []
        )
        envelope = {
            "text": full_text,
            "segments": segments,
            "words": resolved_words,
            "speakers": resolved_speakers,
            "speaker_ranges": eff_speaker_ranges,
            "by_speaker": eff_by_speaker,
            "diarized": diarized,
            "backend": current.get("backend", "whisperx"),
            "diarization_backend": diarization_backend,
            "compute_config": eff_compute_cfg,
        }
        with self._connect() as conn:
            conn.execute(
                "UPDATE transcriptions SET segments_json = ? WHERE id = ? AND project_id = ?",
                (
                    json.dumps(envelope, ensure_ascii=False),
                    current["id"],
                    current["project_id"],
                ),
            )
            conn.commit()
        return self.get_transcription(project_id, current["id"])

    # ------------------------------------------------------------------
    # Identities (InsightFace buffalo_l face registry)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_identity(row: sqlite3.Row) -> dict[str, Any]:
        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "reference_asset_id": row["reference_asset_id"],
            "metadata": meta,
            "created_at": row["created_at"],
        }

    def save_identity(
        self,
        project_id: str,
        *,
        name: str,
        reference_asset_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        identity_id: str | None = None,
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")
        pid = project["id"]
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("El nombre de la identidad no puede estar vacío")

        if reference_asset_id is not None and self.get_asset(pid, reference_asset_id) is None:
            raise ValueError(f"Asset de referencia no encontrado: {reference_asset_id!r}")

        existing = None
        if identity_id:
            existing = self.get_identity(pid, identity_id)
        if existing is None:
            existing = self.get_identity_by_name(pid, cleaned_name)

        now = _utc_now_iso()
        if existing is not None:
            iid = existing["id"]
            new_ref = (
                reference_asset_id
                if reference_asset_id is not None
                else existing["reference_asset_id"]
            )
            merged_meta = dict(existing.get("metadata") or {})
            if metadata:
                merged_meta.update(metadata)
            with self._connect() as conn:
                conn.execute(
                    "UPDATE identities SET name = ?, reference_asset_id = ?, metadata_json = ? "
                    "WHERE id = ? AND project_id = ?",
                    (
                        cleaned_name,
                        new_ref,
                        json.dumps(merged_meta, ensure_ascii=False),
                        iid,
                        pid,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM identities WHERE id = ?",
                    (iid,),
                ).fetchone()
            assert row is not None
            return self._row_to_identity(row)

        iid = (
            self._validate_project_id(identity_id)
            if identity_id
            else f"id-{uuid.uuid4().hex[:12]}"
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO identities "
                "(id, project_id, name, reference_asset_id, metadata_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    iid,
                    pid,
                    cleaned_name,
                    reference_asset_id,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM identities WHERE id = ?",
                (iid,),
            ).fetchone()
        assert row is not None
        return self._row_to_identity(row)

    def rename_identity(
        self,
        project_id: str,
        identity_id: str,
        name: str = "",
    ) -> dict[str, Any] | None:
        cleaned_name = (name or "").strip()
        if not cleaned_name:
            raise ValueError("El nombre de la identidad no puede estar vacío")
        current = self.get_identity(project_id, identity_id)
        if current is None:
            return None
        with self._connect() as conn:
            conn.execute(
                "UPDATE identities SET name = ? WHERE id = ? AND project_id = ?",
                (cleaned_name, current["id"], current["project_id"]),
            )
            conn.commit()
        return self.get_identity(project_id, current["id"])

    def get_identity(self, project_id: str, identity_id: str) -> dict[str, Any] | None:
        try:
            pid = self._validate_project_id(project_id)
            iid = self._validate_project_id(identity_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM identities WHERE id = ? AND project_id = ?",
                (iid, pid),
            ).fetchone()
        return self._row_to_identity(row) if row is not None else None

    def get_identity_by_name(self, project_id: str, name: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        target = (name or "").strip().lower()
        if not target:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM identities WHERE project_id = ? ORDER BY created_at ASC",
                (pid,),
            ).fetchall()
        for r in rows:
            if str(r["name"] or "").strip().lower() == target:
                return self._row_to_identity(r)
        return None

    def list_identities(self, project_id: str) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM identities WHERE project_id = ? ORDER BY created_at ASC, id ASC",
                (pid,),
            ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def delete_identity(self, project_id: str, identity_id: str) -> bool:
        current = self.get_identity(project_id, identity_id)
        if current is None:
            return False
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM identities WHERE id = ? AND project_id = ?",
                (current["id"], current["project_id"]),
            )
            conn.commit()
        return True

    # ------------------------------------------------------------------
    # Plans (planner del agente, M3-B) y project_meta (resúmenes de memoria)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_plan(row: sqlite3.Row) -> dict[str, Any]:
        try:
            steps = json.loads(row["steps_json"] or "[]")
        except json.JSONDecodeError:
            steps = []
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "title": row["title"],
            "steps": steps if isinstance(steps, list) else [],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_plan(
        self,
        project_id: str,
        *,
        title: str = "",
        steps: list[dict[str, Any]] | None = None,
        status: str = "active",
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """Crea o actualiza (upsert por `plan_id`) un plan del proyecto."""
        if self.get_project(project_id) is None:
            raise LookupError(f"Proyecto '{project_id}' no encontrado")
        pid = self._validate_project_id(project_id)
        cleaned_steps = [
            {
                "text": str(step.get("text") or "").strip(),
                "status": str(step.get("status") or "pending").strip() or "pending",
            }
            for step in (steps or [])
            if isinstance(step, dict)
        ]
        now = _utc_now_iso()
        target_id = str(plan_id).strip() if plan_id else f"plan-{uuid.uuid4().hex[:12]}"
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM plans WHERE id = ? AND project_id = ?",
                (target_id, pid),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE plans SET title = ?, steps_json = ?, status = ?, updated_at = ? "
                    "WHERE id = ? AND project_id = ?",
                    (
                        (title or "").strip(),
                        json.dumps(cleaned_steps, ensure_ascii=False),
                        status,
                        now,
                        target_id,
                        pid,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO plans "
                    "(id, project_id, title, steps_json, status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        target_id,
                        pid,
                        (title or "").strip(),
                        json.dumps(cleaned_steps, ensure_ascii=False),
                        status,
                        now,
                        now,
                    ),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM plans WHERE id = ? AND project_id = ?",
                (target_id, pid),
            ).fetchone()
        return self._row_to_plan(row)

    def get_plan(self, project_id: str, plan_id: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE id = ? AND project_id = ?",
                (str(plan_id).strip(), pid),
            ).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def get_active_plan(self, project_id: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE project_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def list_plans(self, project_id: str) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM plans WHERE project_id = ? ORDER BY updated_at DESC, rowid DESC",
                (pid,),
            ).fetchall()
        return [self._row_to_plan(r) for r in rows]

    def set_meta(self, project_id: str, key: str, value: Any) -> None:
        """Guarda un valor JSON asociado a una clave del proyecto (upsert)."""
        pid = self._validate_project_id(project_id)
        clean_key = str(key).strip()
        if not clean_key:
            raise ValueError("La clave de metadato no puede estar vacía")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO project_meta (project_id, key, value_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, key) DO UPDATE SET "
                "value_json = excluded.value_json, updated_at = excluded.updated_at",
                (pid, clean_key, json.dumps(value, ensure_ascii=False), _utc_now_iso()),
            )
            conn.commit()

    def get_meta(self, project_id: str, key: str, default: Any = None) -> Any:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value_json FROM project_meta WHERE project_id = ? AND key = ?",
                (pid, str(key).strip()),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"] or "null")
        except json.JSONDecodeError:
            return default

