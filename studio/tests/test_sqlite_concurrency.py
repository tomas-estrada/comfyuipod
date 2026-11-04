"""Tests de contención: escrituras concurrentes sobre studio.db, checkpoints e index.db.

El loop SSE del agente, el dispatcher de webhooks y las tareas de percepción
escriben a la vez (herramientas `parallel_safe=True`, reindex vía
`run_in_executor`). WAL + `busy_timeout` deben impedir `database is locked`
(M3-IDEA-016) y el context manager `_connection` debe cerrar SIEMPRE la
conexión (antes `with sqlite3.connect(...)` solo hacía commit, nunca close).
"""

from __future__ import annotations

import concurrent.futures
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.state import ProductionState
from comfy_studio.projects import ProjectStore

THREADS = 8
OPS_PER_THREAD = 25


def _run_threads(fn) -> list[BaseException]:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            fn(n)
        except BaseException as exc:  # noqa: BLE001 - recogemos todo para fallar al final
            errors.append(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(worker, range(THREADS)))
    return errors


def test_connection_context_manager_always_closes(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)
    with store._connection() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")

    orch_dir = studio_data_dir / "orch"
    orch_dir.mkdir(parents=True, exist_ok=True)
    cp = ProductionCheckpointer(orch_dir / "checkpoints.sqlite3")
    with cp._connection() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_project_store_concurrent_writes_no_locked_errors(studio_data_dir: Path) -> None:
    store = ProjectStore(studio_data_dir)

    def worker(n: int) -> None:
        proj = store.create_project(name=f"p{n}")
        for i in range(OPS_PER_THREAD):
            job = store.create_job(kind="render", project_id=proj["id"], params={"i": i})
            store.update_job(job["id"], status="running", progress_value=i)

    errors = _run_threads(worker)
    assert errors == []
    assert len(store.list_jobs()) == THREADS * OPS_PER_THREAD
    with closing(sqlite3.connect(studio_data_dir / "studio.db")) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_checkpointer_concurrent_save_load(studio_data_dir: Path) -> None:
    orch_dir = studio_data_dir / "orch"
    orch_dir.mkdir(parents=True, exist_ok=True)
    cp = ProductionCheckpointer(orch_dir / "checkpoints.sqlite3")

    def worker(n: int) -> None:
        for i in range(OPS_PER_THREAD):
            pid = f"prod-{n}-{i}"
            cp.save(ProductionState(idea=f"idea-{n}-{i}", id=pid, phase="executing"))
            loaded = cp.load(pid)
            assert loaded is not None
            assert loaded.idea == f"idea-{n}-{i}"

    errors = _run_threads(worker)
    assert errors == []
    assert len(cp.list_ids(limit=THREADS * OPS_PER_THREAD)) == THREADS * OPS_PER_THREAD
    with closing(sqlite3.connect(cp.db_path)) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_vector_store_concurrent_upserts_no_locked_errors(studio_data_dir: Path) -> None:
    pytest.importorskip("sqlite_vec")
    from comfy_studio.perception.store import CLIP_EMBED_DIM, PerceptionVectorStore

    vs = PerceptionVectorStore(studio_data_dir / "index.db")

    def worker(n: int) -> None:
        for i in range(OPS_PER_THREAD):
            vs.upsert_clip_shot(
                project_id="p1",
                source_asset_id="asset-src",
                keyframe_asset_id=f"kf-{n}-{i}",
                timestamp_sec=float(i),
                shot_start_sec=0.0,
                shot_end_sec=1.0,
                embedding=[(n + 1) * 0.001 + i * 0.0001] * CLIP_EMBED_DIM,
            )

    errors = _run_threads(worker)
    assert errors == []
    with closing(sqlite3.connect(str(vs.index_db_path))) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM clip_shots").fetchone()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert count == THREADS * OPS_PER_THREAD
    assert str(mode).lower() == "wal"
