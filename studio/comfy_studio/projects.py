"""Persistencia SQLite (studio.db) y gestión de proyectos, jobs y medios."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("comfy_studio.projects")
#: Compatibilidad: el objetivo por defecto sigue siendo un entero de GB, pero
#: la arquitectura vigente es una RTX PRO 6000 Blackwell de 96 GB con perfiles de
#: RAM de 128/256 GB (ver `hardware_profile`). Se mantiene el nombre histórico
#: porque `assess_vram_fit` lo usa como valor por omisión.
from comfy_studio.hardware_profile import (  # noqa: E402
    DEFAULT_PROFILE,
    HardwareProfile,
)

TARGET_VRAM_GB = DEFAULT_PROFILE.vram_gb

#: Orden en el que la política dice investigar antes de subir de hardware.
#: Cada una es específica del modelo: la política prohíbe aplicar flags
#: genéricos a ciegas, tratar RAM como intercambiable con VRAM o afirmar
#: compatibilidad universal. Por eso ninguna se marca como «sirve»: hay que
#: medirla.
MEMORY_OPTIMISATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "precision_quantization",
        "label": "Precisión y cuantización adecuadas",
        "note": (
            "fp8/fp16 según soporte real del modelo; reducir la precisión "
            "puede degradar la calidad."
        ),
    },
    {
        "id": "sequential_offload",
        "label": "Descarga secuencial (sequential scheduling y unloading)",
        "note": "Ya implementada en el cliente: se libera la VRAM cuando no queda trabajo.",
    },
    {
        "id": "model_group_offload",
        "label": "Offloading de modelo o grupo",
        "note": (
            "Mueve pesos a RAM de host; más lento, y la RAM de host no es "
            "intercambiable con VRAM."
        ),
    },
    {
        "id": "cpu_components",
        "label": "Componentes compatibles en CPU",
        "note": (
            "Solo para partes que el pipeline permita (p. ej. VAE, tokenizador)."
        ),
    },
    {
        "id": "vae_tiling",
        "label": "VAE tiling y otras optimizaciones de memoria soportadas",
        "note": "Reduce el pico de VRAM del VAE a cambio de tiempo y posibles costuras.",
    },
    {
        "id": "workload_params",
        "label": "Parámetros de carga coherentes con la calidad pedida",
        "note": (
            "Resolución, frames y batch; bajar la calidad no es aceptable "
            "salvo petición."
        ),
    },
)

SCHEMA_VERSION = 3
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
    # 3D / Malla
    "model/gltf-binary": ("model3d", {".glb"}),
    "model/gltf+json": ("model3d", {".gltf"}),
    "model/obj": ("model3d", {".obj"}),
}

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Estados de una medición de render. `recipe_for_template` solo acepta «ok»:
#: una medición perdida (muestreador de `/system_stats` agotado o roto) tiene
#: que salir de ese filtrado, o se contaría como una medición buena y las recetas
#: juzgarían `assess_vram_fit` sin dato.
#:
#: El esquema limita `status` a `('ok','error')` con un CHECK, así que la
#: medición perdida se marca con el único estado no-ok que existe; el motivo
#: concreto va en la columna `error`. Un valor propio («unmeasured») exigiría
#: migrar el CHECK de `render_measurements`, que no es el objeto de este cambio.
MEASUREMENT_STATUS_OK = "ok"
MEASUREMENT_STATUS_LOST = "error"

#: Estados TERMINALES de un job: quien los ha escrito ya no va a volver a
#: tocarlos. Es el contrato COMPLETO de terminalidad y vive aquí, en un solo
#: sitio, porque lo consumen tres lectores que no deben poder divergir:
#: `ProjectStore.has_pending_work` (¿descargo los modelos?), `recovery.py`
#: (`STALE_STATUSES`, el vocabulario en vuelo) y `routers/core.py` (`jobs_active`).
#:
#: `failed` es terminal de verdad: lo escriben cuatro sitios de producción
#: (`routers/ads.py` en las tres rutas de `AdPipelineError` y `agent/tools.py`
#: en el motor de ads) justo después de fallar, y ninguno de ellos actualiza el
#: job después. Mientras no estuviera en esta lista, un job de ads fallado
#: contaba como «en vuelo» para siempre: `has_pending_work()` nunca más daba
#: False y `POST /free` dejaba de llamarse durante el resto de la vida del pod,
#: sin un solo aviso en el log.
TERMINAL_STATUSES: tuple[str, ...] = (
    "done",
    "error",
    "cancelled",
    "interrupted",
    "failed",
)


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

-- M7 (Worker 7): briefs creativos versionados por proyecto (append-only: cada
-- mutación inserta una nueva versión; `changes_json` describe el delta).
CREATE TABLE IF NOT EXISTS creative_briefs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft',
    prompt TEXT NOT NULL DEFAULT '',
    brief_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    changes_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_project_id ON assets(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_project_id ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_chat_project_id ON chat_messages(project_id);
CREATE INDEX IF NOT EXISTS idx_plans_project_id ON plans(project_id);
CREATE INDEX IF NOT EXISTS idx_creative_briefs_project_id ON creative_briefs(project_id);
"""


SCHEMA_V2_SQL = """
CREATE TABLE IF NOT EXISTS review_pins (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    edl_version INTEGER NOT NULL,
    t_start REAL NOT NULL,
    t_end REAL NOT NULL,
    severity TEXT NOT NULL DEFAULT 'note' CHECK (severity IN ('note','issue','blocker')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved','discarded')),
    body TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT 'reviewer',
    resolved_by TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_pins_project
    ON review_pins(project_id, edl_version, status);
"""

SCHEMA_V3_SQL = """
-- Política de arquitectura (punto 5): recetas de ejecución con resultados
-- MEDIDOS. Sin esto, cada decisión de parámetros se toma desde cero a partir de
-- estimativas estáticas. `vram_peak_mb`/`ram_peak_mb`/`duration_s` son medidas,
-- no declaraciones.
CREATE TABLE IF NOT EXISTS render_measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id TEXT NOT NULL,
    job_id TEXT,
    project_id TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    -- Medidas
    duration_s REAL,
    vram_peak_mb INTEGER,
    ram_peak_mb INTEGER,
    -- Entorno medido
    gpu_name TEXT,
    driver TEXT,
    cuda TEXT,
    -- Resultado
    status TEXT NOT NULL CHECK (status IN ('ok','error')),
    error TEXT,
    simulated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_render_measurements_template
    ON render_measurements(template_id, created_at DESC);
"""



def _usable_after_reserve(gb: float, reserve_gb: float) -> float:
    """GB utilizables de una cifra **anunciada** tras la reserva operativa.

    La reserva no es un detalle del perfil por defecto: se aplica igual a un
    override manual, porque también él son GB del datasheet.
    """
    return max(float(gb) - float(reserve_gb), 0.0)


def assess_vram_fit(
    *,
    vram_peak_mb: int | None,
    ram_peak_mb: int | None = None,
    profile: HardwareProfile | None = None,
    target_vram_gb: float | None = None,
) -> dict[str, Any]:
    """¿Cabe la receta medida en el perfil seleccionado, y qué probar si no?

    Punto 5: «Before recommending more expensive hardware, investigate supported
    combinations of…». Compara contra la VRAM **utilizable** del perfil (tras la
    reserva operativa), no contra los GB del datasheet.

    El perfil se pasa explícitamente: si no se propaga, una receta de 40 GB se
    marca como «no cabe» en un perfil de 96 GB. `target_vram_gb` queda como
    override manual para no rompler llamadas antiguas; es una cifra **anunciada**
    (como `vram_gb`), así que se le aplica la misma reserva operativa y el
    payload publica el número contra el que se juzgó de verdad
    (`judged_against_gb`).

    No afirma que una optimización funcione: son específicas del modelo y hay que
    medirlas. La RAM se evalúa aparte (`assess_ram_fit`): no es intercambiable
    con VRAM.
    """
    active = profile or DEFAULT_PROFILE
    # El override manual se trata como los GB del datasheet: se le resta la
    # misma reserva operativa del perfil antes de comparar. Sin override, la
    # base anunciada y la utilizable son las del perfil.
    if target_vram_gb is not None:
        advertised_gb: float = float(target_vram_gb)
        judged_gb = _usable_after_reserve(advertised_gb, active.vram_reserve_gb)
    else:
        advertised_gb = float(active.vram_gb)
        judged_gb = active.vram_usable_gb
    target_mb = int(judged_gb * 1024)
    result: dict[str, Any] = {
        "profile": active.name,
        "target_vram_gb": (
            int(advertised_gb) if target_vram_gb is None else advertised_gb
        ),
        "target_vram_usable_gb": judged_gb,
        # Número contra el que se compara la medición, dicho sin ambigüedad:
        # con override ya no es el `*_usable_gb` del perfil.
        "judged_against_gb": judged_gb,
        "target_vram_mb": target_mb,
        "measured": vram_peak_mb is not None,
        "vram_peak_mb": vram_peak_mb,
        # La RAM medida viaja con la receta: hay que verificarla en cada oferta,
        # pero su veredicto vive en `assess_ram_fit`, no aquí.
        "ram_peak_mb": ram_peak_mb,
        "fits": None,
        "headroom_mb": None,
        "utilisation_pct": None,
        "optimisations": [],
    }
    if vram_peak_mb is None:
        # Sin medición no se puede afirmar nada: la política pide medidas.
        result["note"] = "sin medición de VRAM todavía; ejecuta una vez para medir"
        return result

    result["fits"] = vram_peak_mb <= target_mb
    result["headroom_mb"] = target_mb - vram_peak_mb
    if target_mb > 0:
        result["utilisation_pct"] = round(100.0 * vram_peak_mb / target_mb, 1)
    # Con 0 GB utilizables (override por debajo de la reserva) no hay porcentaje
    # que calcular: se dice que no cabe y ya.
    if result["fits"]:
        return result

    # No cabe: se listan las opciones en el orden de la política. Ninguna se
    # marca como válida sin medirla.
    result["optimisations"] = [dict(o) for o in MEMORY_OPTIMISATIONS]
    # La nota cita el número que se usó de verdad, no el que anuncia el perfil.
    basis = f"{judged_gb:.0f} GB utilizables de {active.name}"
    if target_vram_gb is not None:
        basis += f" (override de {advertised_gb:.0f} GB menos la reserva operativa)"
    result["note"] = (
        f"no cabe en {basis}; medir estas opciones antes de recomendar "
        "hardware más caro"
    )
    return result


def assess_ram_fit(
    *,
    ram_peak_mb: int | None,
    profile: HardwareProfile | None = None,
    target_ram_gb: float | None = None,
) -> dict[str, Any]:
    """Veredicto de RAM de host, separado del de VRAM.

    La política pide «sufficient host RAM for the selected workload» y verificar
    la RAM real de cada oferta sin inferirla del modelo de GPU. La RAM de host
    **no** amplía lo que cabe en el GPU: solo permite offloading, a costa de
    tiempo. `target_ram_gb` es un override manual y, como `ram_gb`, una cifra
    anunciada: se le aplica la misma reserva operativa y `judged_against_gb`
    dice contra qué número se juzgó.
    """
    active = profile or DEFAULT_PROFILE
    if target_ram_gb is not None:
        advertised_gb: float = float(target_ram_gb)
        judged_gb = _usable_after_reserve(advertised_gb, active.ram_reserve_gb)
    else:
        advertised_gb = float(active.ram_gb)
        judged_gb = active.ram_usable_gb
    target_mb = int(judged_gb * 1024)
    result: dict[str, Any] = {
        "profile": active.name,
        "target_ram_gb": int(advertised_gb) if target_ram_gb is None else advertised_gb,
        "target_ram_usable_gb": judged_gb,
        "judged_against_gb": judged_gb,
        "target_ram_mb": target_mb,
        "measured": ram_peak_mb is not None,
        "ram_peak_mb": ram_peak_mb,
        "fits": None,
        "headroom_mb": None,
        "utilisation_pct": None,
    }
    if ram_peak_mb is None:
        result["note"] = "sin medición de RAM todavía; ejecuta una vez para medir"
        return result
    result["fits"] = ram_peak_mb <= target_mb
    result["headroom_mb"] = target_mb - ram_peak_mb
    if target_mb > 0:
        result["utilisation_pct"] = round(100.0 * ram_peak_mb / target_mb, 1)
    if not result["fits"]:
        # La nota cita el número que se usó de verdad, no el del perfil.
        basis = f"{judged_gb:.0f} GB utilizables"
        if target_ram_gb is not None:
            basis += (
                f" (override de {advertised_gb:.0f} GB menos la reserva operativa)"
            )
        result["note"] = (
            f"la RAM de host no basta para esta carga: se midieron "
            f"{ram_peak_mb} MB contra {basis}; subir el perfil de RAM "
            "(128 → 256 GB) o reducir la carga, no la VRAM"
        )
    return result



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

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Conexión con cierre garantizado: commit al salir, close siempre.

        `with sqlite3.connect(...)` solo hace commit/rollback, nunca close;
        en hilos del executor el cierre quedaba a merced del GC.
        """
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.executescript(SCHEMA_V1_SQL)
            existing_v1 = conn.execute(
                "SELECT version FROM schema_version WHERE version = 1",
            ).fetchone()
            if existing_v1 is None:
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (
                        1,
                        _utc_now_iso(),
                        "Initial schema: projects, jobs, assets, chat, transcriptions, edl",
                    ),
                )
            # M3-RVP-001: migración a v2 (review_pins). Idempotente: CREATE IF NOT EXISTS.
            existing_v2 = conn.execute(
                "SELECT version FROM schema_version WHERE version = 2",
            ).fetchone()
            if existing_v2 is None:
                conn.executescript(SCHEMA_V2_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (
                        2,
                        _utc_now_iso(),
                        "M3-RVP-001: review_pins para feedback anclado a versiones de EDL",
                    ),
                )
            # Política de arquitectura: mediciones de render (recetas medidas).
            existing_v3 = conn.execute(
                "SELECT version FROM schema_version WHERE version = 3",
            ).fetchone()
            if existing_v3 is None:
                conn.executescript(SCHEMA_V3_SQL)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) "
                    "VALUES (?, ?, ?)",
                    (
                        3,
                        _utc_now_iso(),
                        "Política de recursos: render_measurements (VRAM/RAM/tiempo medidos)",
                    ),
                )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")
            conn.commit()
    def get_schema_version(self) -> int:
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        try:
            safe_id = self._validate_project_id(project_id)
        except ValueError:
            return None
        with self._connection() as conn:
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

        with self._connection() as conn:
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
        with self._connection() as conn:
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

        idempotency_key = str((metadata or {}).get("idempotency_key") or "").strip()
        if idempotency_key and not job_id:
            raise ValueError("Una carga idempotente requiere job_id")
        asset_id = (
            hashlib.sha256(
                f"{project_id}\0{job_id}\0{idempotency_key}".encode()
            ).hexdigest()[:32]
            if idempotency_key
            else uuid.uuid4().hex[:12]
        )
        stored_filename = f"{asset_id}_{safe_name}"
        pdir = self._ensure_project_dirs(project_id)
        category_dir = (pdir / category).resolve()
        target_path = (category_dir / stored_filename).resolve()
        if not target_path.is_relative_to(category_dir):
            raise MediaValidationError(
                "Nombre de archivo rechazado por política de ruta.", status_code=400
            )

        existing = self.get_asset(project_id, asset_id) if idempotency_key else None
        if existing is not None:
            stored_meta = existing.get("metadata") or {}
            existing_path = (self.data_dir / str(existing.get("rel_path") or "")).resolve()
            if (
                stored_meta.get("idempotency_key") == idempotency_key
                and existing_path.is_relative_to(self.data_dir)
                and existing_path.is_file()
                and existing_path.read_bytes() == data
            ):
                return existing
            raise MediaValidationError(
                "La clave idempotente ya tiene un artefacto distinto.", status_code=409
            )
        if target_path.exists():
            if not idempotency_key or target_path.read_bytes() != data:
                raise MediaValidationError(
                    "El archivo de staging de la carga no coincide.", status_code=409
                )
        else:
            target_path.write_bytes(data)
        rel_path = target_path.relative_to(self.data_dir).as_posix()
        now = _utc_now_iso()

        try:
            with self._connection() as conn:
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
        except Exception:
            # No dejar el archivo huérfano en disco si la transacción falla
            # (p. ej. job_id que no existe viola la FK y aborta el INSERT).
            target_path.unlink(missing_ok=True)
            raise
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        job_id: str | None = None,
        project_id: str | None = None,
        template_id: str | None = None,
        prompt_id: str | None = None,
        params: dict[str, Any] | None = None,
        status: str = "queued",
    ) -> dict[str, Any]:
        job_id = self._validate_project_id(job_id) if job_id else uuid.uuid4().hex[:12]
        now = _utc_now_iso()
        with self._connection() as conn:
            conn.execute(
                "INSERT INTO jobs "
                "(id, project_id, kind, status, template_id, prompt_id, "
                "progress_value, progress_max, current_node, params_json, "
                "result_json, error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO NOTHING",
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM jobs {where_sql} ORDER BY created_at DESC, id DESC",
                params,
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def has_pending_work(self, exclude_job_id: str | None = None) -> bool:
        """True si queda algún job en estado NO terminal (salvo `exclude_job_id`).

        Es una consulta de EXISTENCIA: un único `SELECT ... LIMIT 1` que no
        materializa ninguna fila. El guard que la usa decide si descargar los
        modelos tras un render, y se ejecuta bajo el cerrojo GPU; la alternativa
        (`list_jobs()` completo) recorría la tabla entera y `_row_to_job` hace
        `json.loads` de `params_json` y `result_json` por fila, así que la cola
        de renders esperaba detrás de un barrido de SQLite que crece con cada
        render.

        La consulta es de EXISTENCIA sobre estados TERMINALES, no una lista de
        estados en vuelo. La lista negra era fail-OPEN: bastaba un estado que no
        estuviera enumerado para que la consulta respondiera «no queda nada» y se
        descargaran los modelos con un job en vuelo. Pasó con `fetching`, que
        `recovery.py` y `routers/core.py` tratan como job activo. Con la lista
        blanca ocurre lo contrario y en el sentido seguro: un estado desconocido
        —uno nuevo, o uno antiguo de una versión previa— queda como «en vuelo» y
        no se descarga nada.

        La lista blanca es `TERMINAL_STATUSES`, UNA constante de módulo: el
        vocabulario en vuelo de `recovery.py`/`routers/core.py` y este conjunto
        tienen que ser complementarios, y un estado terminal que se olvidara
        aquí (`failed`, por ejemplo) desactivaría `POST /free` para siempre.

        Sigue siendo UN `SELECT ... LIMIT 1` que no materializa ninguna fila. La
        consulta no usa índice sobre `status` (la tabla no lo tiene), así que lo
        acotado es el trabajo que devuelve, no el barrido: con la cola larga, el
        `LIMIT 1` corta en cuanto aparece la primera fila en vuelo.
        """
        terminal_statuses = TERMINAL_STATUSES
        placeholders = ", ".join("?" for _ in terminal_statuses)
        sql = f"SELECT 1 FROM jobs WHERE status NOT IN ({placeholders})"
        query: list[Any] = list(terminal_statuses)
        if exclude_job_id:
            sql += " AND id <> ?"
            query.append(exclude_job_id)
        with self._connection() as conn:
            row = conn.execute(f"{sql} LIMIT 1", query).fetchone()
        return row is not None

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

        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM chat_messages WHERE project_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (pid, max(1, int(limit))),
            ).fetchall()
        return [self._row_to_chat_message(r) for r in reversed(rows)]

    def clear_chat_messages(self, project_id: str) -> int:
        pid = self._validate_project_id(project_id)
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
    # Review pins (M3-RVP-001): feedback anclado a versiones de EDL
    # ------------------------------------------------------------------

    PIN_SEVERITIES: tuple[str, ...] = ("note", "issue", "blocker")
    PIN_STATUSES: tuple[str, ...] = ("open", "resolved", "discarded")
    PIN_BODY_MAX_CHARS = 2000
    PIN_RESOLUTION_MAX_CHARS = 1000

    @staticmethod
    def _row_to_review_pin(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "edl_version": int(row["edl_version"]),
            "t_start": float(row["t_start"]),
            "t_end": float(row["t_end"]),
            "severity": row["severity"],
            "status": row["status"],
            "body": row["body"],
            "author": row["author"],
            "resolved_by": row["resolved_by"],
            "resolution_note": row["resolution_note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_review_pin(
        self,
        project_id: str,
        *,
        edl_version: int,
        t_start: float,
        t_end: float,
        severity: str,
        body: str,
        author: str = "reviewer",
    ) -> dict[str, Any]:
        project = self.get_project(project_id)
        if project is None:
            raise ValueError(f"Proyecto no encontrado: {project_id!r}")
        pid = project["id"]
        if severity not in self.PIN_SEVERITIES:
            raise ValueError(
                f"Severidad inválida: {severity!r}. Debe ser una de {self.PIN_SEVERITIES}."
            )
        clean_body = (body or "").strip()
        if not clean_body:
            raise ValueError("El cuerpo del pin no puede estar vacío.")
        if len(clean_body) > self.PIN_BODY_MAX_CHARS:
            raise ValueError(
                f"El cuerpo del pin excede {self.PIN_BODY_MAX_CHARS} caracteres."
            )
        with self._connect() as conn:
            version_row = conn.execute(
                "SELECT 1 FROM edl_versions WHERE project_id = ? AND version = ?",
                (pid, int(edl_version)),
            ).fetchone()
            if version_row is None:
                raise ValueError(
                    f"Versión de EDL no encontrada: {edl_version!r} "
                    f"en proyecto {pid!r}"
                )
            pin_id = f"pin-{uuid.uuid4().hex[:12]}"
            now = _utc_now_iso()
            conn.execute(
                "INSERT INTO review_pins "
                "(id, project_id, edl_version, t_start, t_end, severity, status, "
                "body, author, resolved_by, resolution_note, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL, NULL, ?, ?)",
                (
                    pin_id,
                    pid,
                    int(edl_version),
                    float(t_start),
                    float(t_end),
                    severity,
                    clean_body,
                    author,
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM review_pins WHERE id = ?",
                (pin_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_review_pin(row)

    def get_review_pin(self, project_id: str, pin_id: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM review_pins WHERE project_id = ? AND id = ?",
                (pid, pin_id),
            ).fetchone()
        return self._row_to_review_pin(row) if row is not None else None

    def list_review_pins(
        self,
        project_id: str,
        *,
        edl_version: int | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        if status is not None and status not in self.PIN_STATUSES:
            raise ValueError(
                f"Estado inválido: {status!r}. Debe ser uno de {self.PIN_STATUSES}."
            )
        query = "SELECT * FROM review_pins WHERE project_id = ?"
        params: list[Any] = [pid]
        if edl_version is not None:
            query += " AND edl_version = ?"
            params.append(int(edl_version))
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY t_start ASC, created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_review_pin(r) for r in rows]

    def update_review_pin(
        self,
        project_id: str,
        pin_id: str,
        *,
        t_start: float | None = None,
        t_end: float | None = None,
        status: str | None = None,
        resolution_note: str | None = None,
        resolved_by: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get_review_pin(project_id, pin_id)
        if current is None:
            return None
        if status is not None and status not in self.PIN_STATUSES:
            raise ValueError(
                f"Estado inválido: {status!r}. Debe ser uno de {self.PIN_STATUSES}."
            )
        new_status = status if status is not None else current["status"]
        if new_status != "open" and current["status"] != "open":
            raise ValueError(
                f"El pin {pin_id!r} ya está en estado {current['status']!r}; "
                "solo los pins abiertos pueden cambiar de estado."
            )
        if resolution_note is not None and len(resolution_note) > self.PIN_RESOLUTION_MAX_CHARS:
            raise ValueError(
                f"La nota de resolución excede {self.PIN_RESOLUTION_MAX_CHARS} caracteres."
            )
        new_t_start = (
            float(t_start) if t_start is not None else current["t_start"]
        )
        new_t_end = float(t_end) if t_end is not None else current["t_end"]
        if new_t_start > new_t_end:
            raise ValueError("t_start no puede ser mayor que t_end.")
        clean_note = resolution_note
        if clean_note is not None:
            clean_note = clean_note.strip() or None
        sets: list[str] = [
            "t_start = ?",
            "t_end = ?",
            "status = ?",
            "resolution_note = ?",
            "updated_at = ?",
        ]
        params: list[Any] = [new_t_start, new_t_end, new_status, clean_note]
        if new_status != "open":
            params.append(resolved_by or "reviewer")
            sets.insert(len(sets) - 1, "resolved_by = ?")
        else:
            params.append(current["resolved_by"])
            sets.insert(len(sets) - 1, "resolved_by = ?")
        params.extend([_utc_now_iso(), current["project_id"], current["id"]])
        with self._connect() as conn:
            conn.execute(
                f"UPDATE review_pins SET {', '.join(sets)} "
                "WHERE project_id = ? AND id = ?",
                params,
            )
            conn.commit()
        return self.get_review_pin(project_id, pin_id)

    def delete_review_pin(self, project_id: str, pin_id: str) -> bool:
        current = self.get_review_pin(project_id, pin_id)
        if current is None:
            return False
        if current["status"] != "open":
            raise ValueError(
                f"Solo se pueden eliminar pins abiertos; {pin_id!r} está en "
                f"estado {current['status']!r}."
            )
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM review_pins WHERE project_id = ? AND id = ?",
                (current["project_id"], pin_id),
            )
            conn.commit()
        return cur.rowcount > 0

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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
            with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM identities WHERE project_id = ? ORDER BY created_at ASC, id ASC",
                (pid,),
            ).fetchall()
        return [self._row_to_identity(r) for r in rows]

    def delete_identity(self, project_id: str, identity_id: str) -> bool:
        current = self.get_identity(project_id, identity_id)
        if current is None:
            return False
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE id = ? AND project_id = ?",
                (str(plan_id).strip(), pid),
            ).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def get_active_plan(self, project_id: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM plans WHERE project_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return self._row_to_plan(row) if row is not None else None

    def list_plans(self, project_id: str) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connection() as conn:
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
        with self._connection() as conn:
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
        with self._connection() as conn:
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

    # --- Briefs creativos (M7, Worker 7) ---

    def _row_to_creative_brief(self, row: sqlite3.Row) -> dict[str, Any]:
        def _loads(raw: str, fallback: Any) -> Any:
            try:
                return json.loads(raw or "null") if raw else fallback
            except json.JSONDecodeError:
                return fallback

        return {
            "id": row["id"],
            "project_id": row["project_id"],
            "version": int(row["version"]),
            "status": row["status"],
            "prompt": row["prompt"],
            "brief": _loads(row["brief_json"], {}),
            "evidence": _loads(row["evidence_json"], {}),
            "changes": _loads(row["changes_json"], []),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def save_creative_brief(
        self,
        project_id: str,
        *,
        prompt: str,
        brief: dict[str, Any],
        evidence: dict[str, Any] | None = None,
        changes: list[dict[str, Any]] | None = None,
        status: str = "draft",
    ) -> dict[str, Any]:
        """Inserta una NUEVA versión del brief creativo (append-only).

        `changes` describe el delta respecto a la versión anterior (fuente
        `user` para decisiones humanas, `system` para inferencias).
        """
        project = self.get_project(project_id)
        if project is None:
            raise LookupError(f"Proyecto '{project_id}' no encontrado")
        pid = project["id"]
        now = _utc_now_iso()
        with self._connect() as conn:
            row_ver = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS max_ver "
                "FROM creative_briefs WHERE project_id = ?",
                (pid,),
            ).fetchone()
            next_version = int(row_ver["max_ver"] if row_ver else 0) + 1
            brief_id = f"brief-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO creative_briefs "
                "(id, project_id, version, status, prompt, brief_json, evidence_json, "
                "changes_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    brief_id,
                    pid,
                    next_version,
                    status.strip() or "draft",
                    (prompt or "").strip(),
                    json.dumps(brief or {}, ensure_ascii=False),
                    json.dumps(evidence or {}, ensure_ascii=False),
                    json.dumps(changes or [], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM creative_briefs WHERE id = ? AND project_id = ?",
                (brief_id, pid),
            ).fetchone()
        assert row is not None
        return self._row_to_creative_brief(row)

    def get_creative_brief(
        self,
        project_id: str,
        version: int | str,
    ) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            if isinstance(version, int) or str(version).isdigit():
                row = conn.execute(
                    "SELECT * FROM creative_briefs WHERE project_id = ? AND version = ? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (pid, int(version)),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM creative_briefs WHERE project_id = ? AND id = ?",
                    (pid, str(version).strip()),
                ).fetchone()
        return self._row_to_creative_brief(row) if row is not None else None

    def get_latest_creative_brief(self, project_id: str) -> dict[str, Any] | None:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM creative_briefs WHERE project_id = ? "
                "ORDER BY version DESC, created_at DESC LIMIT 1",
                (pid,),
            ).fetchone()
        return self._row_to_creative_brief(row) if row is not None else None

    def list_creative_briefs(self, project_id: str) -> list[dict[str, Any]]:
        pid = self._validate_project_id(project_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM creative_briefs WHERE project_id = ? "
                "ORDER BY version DESC, created_at DESC",
                (pid,),
            ).fetchall()
        return [self._row_to_creative_brief(r) for r in rows]


    # ------------------------------------------------------------------
    # Mediciones de render (política de arquitectura, punto 5)
    # ------------------------------------------------------------------

    def record_render_measurement(
        self,
        *,
        template_id: str,
        params: dict[str, Any] | None = None,
        job_id: str | None = None,
        project_id: str | None = None,
        duration_s: float | None = None,
        vram_peak_mb: int | None = None,
        ram_peak_mb: int | None = None,
        gpu_name: str | None = None,
        driver: str | None = None,
        cuda: str | None = None,
        status: str = "ok",
        error: str | None = None,
        simulated: bool = False,
    ) -> None:
        """Guarda una medición real de render.

        Escribir una medición nunca debe tumbar un render: si falla, se registra
        en el log y se sigue.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO render_measurements (template_id, job_id, project_id, "
                    "params_json, duration_s, vram_peak_mb, ram_peak_mb, gpu_name, driver, "
                    "cuda, status, error, simulated, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(template_id),
                        job_id,
                        project_id,
                        json.dumps(params or {}, ensure_ascii=False),
                        duration_s,
                        vram_peak_mb,
                        ram_peak_mb,
                        gpu_name,
                        driver,
                        cuda,
                        status,
                        error,
                        1 if simulated else 0,
                        _utc_now_iso(),
                    ),
                )
        except Exception:  # noqa: BLE001 - la medición nunca rompe el render
            logger.warning("No se pudo guardar la medición de render", exc_info=True)

    def list_render_measurements(
        self,
        *,
        template_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Mediciones recientes, opcionalmente filtradas por plantilla."""
        clauses = []
        params: list[Any] = []
        if template_id:
            clauses.append("template_id = ?")
            params.append(str(template_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM render_measurements {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_measurement(r) for r in rows]

    @staticmethod
    def _recipe_env_key(row: dict[str, Any]) -> tuple:
        """Clave de entorno de una medición: lo que la hace comparable con otra.

        Recetas de resolución, frames, GPU o driver distintos NO son la misma
        receta: agregarlas bajo un único `template_id` mezcla peras con
        manzanas y produce un `vram_peak` engañoso. La política (W02/A02) exige
        «clave completa de receta/entorno y consulta compatible».
        """
        params = row.get("params") or {}
        return (
            row.get("gpu_name"),
            row.get("cuda"),
            params.get("width"),
            params.get("height"),
            params.get("frames") or params.get("length"),
        )

    def recipe_for_template(
        self,
        template_id: str,
        *,
        profile: HardwareProfile | None = None,
        env_key: tuple | None = None,
    ) -> dict[str, Any] | None:
        """Receta medida de una plantilla: ajustes declarados + resultados medidos.

        Es la unidad que la política pide reutilizar en lugar de razonar desde
        cero en cada despliegue.

        Dos reglas de honestidad (W02/A02, defectos P0 #4 y #5):

        - Una medición **simulada** (`simulated=True`) NUNCA entra en la
          selección ni en `vram_fit`: es un doble determinista para tests, no
          una medida de hardware. Se reporta aparte como `simulated_samples`.
        - Solo se agregan mediciones del **mismo entorno** (GPU/driver/
          resolución/frames). Si `env_key` se da, filtra por él; si no, se usa
          el entorno de la medición real más reciente, para no mezclar GPUs ni
          resoluciones distintas bajo una sola etiqueta.
        """
        rows = self.list_render_measurements(template_id=template_id, limit=50)
        if not rows:
            return None
        simulated = [r for r in rows if r.get("simulated")]
        real = [r for r in rows if not r.get("simulated")]
        # P0 #4: los simulados jamas participan en seleccion/validacion.
        ok = [
            r
            for r in real
            if r["status"] == MEASUREMENT_STATUS_OK and r["duration_s"] is not None
        ]
        if not ok:
            # La receta no puede publicar un pico: solo hay mediciones perdidas
            # (o «ok» sin `duration_s`, que tampoco sirven). Sin un «por qué»,
            # `recipes[]` —en `render_recipes` y en GET /studio/api/status— no
            # podía explicarse a sí misma: quien la leía se quedaba con un
            # `measured: False` sin motivo y tenía que ir a buscarlo a la lista
            # paralela `measurements[]`. Se publica el error de la fila no-ok
            # MÁS RECIENTE (`rows` viene ordenado de más nueva a más vieja), que
            # es el mismo `_DRAIN_LOST_REASON` que guarda la fila. Un simulado
            # nunca es la razón de una receta real, así que se busca primero
            # entre las reales y solo en su defecto entre todas.
            lost = next(
                (r for r in real if r["status"] != MEASUREMENT_STATUS_OK),
                None,
            ) or next(
                (r for r in rows if r["status"] != MEASUREMENT_STATUS_OK),
                None,
            )
            return {
                "template_id": template_id,
                "samples": len(real),
                "simulated_samples": len(simulated),
                "measured": False,
                "reason": lost.get("error") if lost is not None else None,
            }
        # P0 #5: agrupar solo el mismo entorno. Sin env_key explicito, se toma
        # el de la medicion real mas reciente como entorno de referencia.
        target_key = env_key if env_key is not None else self._recipe_env_key(ok[0])
        compatible = [r for r in ok if self._recipe_env_key(r) == target_key]
        incompatible_envs = len({self._recipe_env_key(r) for r in ok}) - 1
        ok = compatible
        vram = [r["vram_peak_mb"] for r in ok if r["vram_peak_mb"] is not None]
        durations = [r["duration_s"] for r in ok if r["duration_s"] is not None]
        latest = ok[0]
        return {
            "template_id": template_id,
            "samples": len(ok),
            "simulated_samples": len(simulated),
            "env_key": list(target_key),
            "incompatible_env_samples_excluded": incompatible_envs,
            "measured": True,
            "last": latest,
            "vram_peak_mb_max": max(vram) if vram else None,
            "vram_peak_mb_avg": int(sum(vram) / len(vram)) if vram else None,
            "duration_s_last": latest["duration_s"],
            "duration_s_avg": round(sum(durations) / len(durations), 2) if durations else None,
            "duration_s_max": max(durations) if durations else None,
            "gpu_name": latest.get("gpu_name"),
            "driver": latest.get("driver"),
            "cuda": latest.get("cuda"),
            # El perfil se propaga: sin él, una receta de 40 GB se marcaría
            # «no cabe» contra el objetivo por omisión aunque el perfil real
            # tenga 96 GB. RAM y VRAM se juzgan por separado.
            "vram_fit": assess_vram_fit(
                vram_peak_mb=max(vram) if vram else None,
                profile=profile,
            ),
            "ram_fit": assess_ram_fit(
                ram_peak_mb=(
                    max(
                        r["ram_peak_mb"]
                        for r in ok
                        if r.get("ram_peak_mb") is not None
                    )
                    if any(r.get("ram_peak_mb") is not None for r in ok)
                    else None
                ),
                profile=profile,
            ),
        }

    @staticmethod
    def _row_to_measurement(row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        data = dict(row)
        try:
            data["params"] = json.loads(data.pop("params_json", "{}") or "{}")
        except (TypeError, ValueError):
            data["params"] = {}
        data["simulated"] = bool(data.get("simulated"))
        return data
