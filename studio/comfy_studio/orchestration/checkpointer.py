"""Checkpointer SQLite para el estado de producción (M6 Bloque 4, fase 12).

Persiste el `ProductionState` tras cada paso del grafo para poder **reanudar tras
un corte** (Eje 4). Mismo criterio que la memoria del cerebro: SQLite de la
stdlib, sin dep nueva, sin segundo proceso, loopback-only (es un fichero local).

NO usa `langgraph.checkpoint.sqlite`: ese saver existe pero ataría esta capa a la
API interna de LangGraph. Guardamos el JSON de `to_dict()` nosotros, de modo que
el estado es legible, auditable y reanudable aunque LangGraph no esté instalado
(el grafo tiene fallback propio — ver `graph.py`).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from comfy_studio.orchestration.state import ProductionState


def _default_checkpoint_dir() -> Path:
    """Dir de checkpoints: env explícito, si no bajo el data-dir del estudio.

    Evita el hardcode de `/workspace` (que rompía en Windows/macOS sin permisos
    sobre la raíz del volumen): reutiliza `_default_studio_data_dir`, que ya
    resuelve el pod Linux, un override por env y un tempdir portable (M6-SCR-006).
    """
    explicit = os.environ.get("STUDIO_ORCH_CHECKPOINT_DIR", "").strip()
    if explicit:
        return Path(explicit)
    # import local: config importa mucho; checkpointer debe ser liviano.
    from comfy_studio.config import _default_studio_data_dir

    return _default_studio_data_dir(os.environ) / "orchestration"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS production_checkpoint (
    production_id TEXT PRIMARY KEY,
    phase         TEXT NOT NULL,
    state_json    TEXT NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_updated
    ON production_checkpoint(updated_at);
"""

#: busy_timeout uniforme (ms) para tolerar lecturas concurrentes de estado.
_BUSY_TIMEOUT_MS = 5000


class ProductionCheckpointer:
    """Guarda/lee el estado de una producción por id en SQLite local."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            base = _default_checkpoint_dir()
            base.mkdir(parents=True, exist_ok=True)
            db_path = base / "checkpoints.sqlite3"
        self.db_path = str(db_path)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        # WAL + busy_timeout: lecturas de estado concurrentes sin bloquear al
        # grafo que escribe tras cada paso (mismo patrón que projects.py).
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS};")
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # -- API -----------------------------------------------------------------

    def save(self, state: ProductionState) -> None:
        """Persiste (upsert) el estado completo de una producción."""
        state.updated_at = time.time()
        payload = json.dumps(state.to_dict(), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO production_checkpoint "
                "(production_id, phase, state_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(production_id) DO UPDATE SET "
                "phase=excluded.phase, state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (state.id, state.phase, payload, state.updated_at),
            )

    def load(self, production_id: str) -> ProductionState | None:
        """Reanuda el estado de una producción, o None si no existe."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM production_checkpoint "
                "WHERE production_id = ?",
                (production_id,),
            ).fetchone()
        if row is None:
            return None
        return ProductionState.from_dict(json.loads(row["state_json"]))

    def list_ids(self, *, limit: int = 50) -> list[str]:
        """Ids de producciones con checkpoint, más recientes primero."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT production_id FROM production_checkpoint "
                "ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [r["production_id"] for r in rows]

    def delete(self, production_id: str) -> bool:
        """Borra el checkpoint de una producción; True si existía."""
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM production_checkpoint WHERE production_id = ?",
                (production_id,),
            )
            return cur.rowcount > 0
