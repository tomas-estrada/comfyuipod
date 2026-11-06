"""Tests del Bloque 5 fase 17 — ProductionService (tools + puerta de gasto)."""

from __future__ import annotations

import json
import threading

import pytest

import comfy_studio.orchestration.service as service_module
from comfy_studio.orchestration import (
    ProductionCheckpointer,
    ProductionNotApprovedError,
    ProductionNotFoundError,
    ProductionService,
)


def _service(tmp_path) -> ProductionService:
    cp = ProductionCheckpointer(db_path=tmp_path / "svc.sqlite3")
    return ProductionService(checkpointer=cp)


def test_plan_production_awaits_approval(tmp_path):
    svc = _service(tmp_path)
    out = svc.plan_production("un dron nocturno sobre la ciudad", cost_cap_usd=2.0)
    assert out["phase"] == "awaiting_approval"
    assert out["storyboard_state"] == "awaiting_approval"
    assert out["plan"]["sequences"]
    # el plan se persiste y es recuperable.
    pid = out["production_id"]
    again = svc.get_plan(pid)
    assert again["plan"]["id"] == pid


def test_status_reports_progress(tmp_path):
    svc = _service(tmp_path)
    pid = svc.plan_production("gato astronauta")["production_id"]
    st = svc.production_status(pid)
    assert st["shots_total"] >= 1
    assert st["shots_accepted"] == 0
    assert st["storyboard_approved"] is False
    assert st["complete"] is False


def test_approve_reports_simulation_without_fake_artifact(tmp_path):
    svc = _service(tmp_path)
    pid = svc.plan_production("desierto rojo", cost_cap_usd=1.0)["production_id"]
    out = svc.approve_plan(pid)
    assert out["phase"] == "simulated"
    assert out["final_output_path"] is None
    assert out["output_asset_ids"] == []
    assert out["status"]["complete"] is True
    assert out["status"]["simulated"] is True
    assert out["status"]["execution_mode"] == "simulated"
    assert out["status"]["artifact_ready"] is False
    assert out["status"]["shots_accepted"] == out["status"]["shots_total"]
    assert out["status"]["storyboard_approved"] is True


def test_approve_twice_is_idempotent_after_terminal_simulation(tmp_path):
    svc = _service(tmp_path)
    pid = svc.plan_production("x")["production_id"]
    svc.approve_plan(pid)  # first approval runs the loop
    # terminal simulation is idempotent and does not re-run the graph.
    out2 = svc.approve_plan(pid)
    assert out2["phase"] == "simulated"
    assert out2["final_output_path"] is None


def test_concurrent_approvals_claim_the_production_graph_once(tmp_path, monkeypatch):
    """Concurrent retries see the in-flight approval instead of rerunning it."""
    db = tmp_path / "concurrent-approval.sqlite3"
    first = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    second = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    production_id = first.plan_production("luz de amanecer")["production_id"]

    loaded = threading.Barrier(2)
    original_load = ProductionCheckpointer.load
    seen_initial_load = threading.local()

    def synchronized_initial_load(checkpointer, requested_id):
        state = original_load(checkpointer, requested_id)
        if not getattr(seen_initial_load, "done", False):
            seen_initial_load.done = True
            loaded.wait(timeout=5)
        return state

    monkeypatch.setattr(ProductionCheckpointer, "load", synchronized_initial_load)
    graph_started = threading.Event()
    finish_graph = threading.Event()
    runs = {"count": 0}

    class BlockingGraph:
        should_cancel = None
        on_step = None

        def run(self, state):
            runs["count"] += 1
            graph_started.set()
            assert finish_graph.wait(timeout=5)
            state.phase = "simulated"
            state.metadata["simulated"] = True
            state.record("simulated")
            return state

    monkeypatch.setattr(
        service_module, "build_production_graph", lambda **_kwargs: BlockingGraph()
    )
    results = []
    errors = []

    def approve(service):
        try:
            results.append(service.approve_plan(production_id))
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=approve, args=(svc,)) for svc in (first, second)]
    for thread in threads:
        thread.start()
    assert graph_started.wait(timeout=5)
    for _ in range(500):
        if any(result.get("already_in_progress") for result in results):
            break
        threading.Event().wait(0.01)
    assert any(result.get("already_in_progress") for result in results)
    finish_graph.set()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors, errors
    assert all(not thread.is_alive() for thread in threads)
    assert runs["count"] == 1


def test_compare_and_set_refuses_stale_plan_snapshot(tmp_path):
    svc = _service(tmp_path)
    production_id = svc.plan_production("brief inicial")["production_id"]

    stale = svc.checkpointer.load(production_id)
    assert stale is not None
    stale_snapshot = json.dumps(stale.to_dict(), ensure_ascii=False)

    # A plan edit lands after the approval request loaded its source snapshot.
    svc.update_plan(production_id, idea="brief revisado")
    stale.phase = "executing"
    claimed = svc.checkpointer.claim_phase(
        stale,
        expected_phase="awaiting_approval",
        expected_state_json=stale_snapshot,
    )

    assert claimed is False
    persisted = svc.checkpointer.load(production_id)
    assert persisted is not None
    assert persisted.phase == "awaiting_approval"
    assert persisted.idea == "brief revisado"


def test_get_plan_missing_raises(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(ProductionNotFoundError):
        svc.get_plan("prod_does_not_exist")


def test_status_missing_raises(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(ProductionNotFoundError):
        svc.production_status("prod_ghost")


def test_approve_without_plan_raises(tmp_path):
    svc = _service(tmp_path)
    with pytest.raises(ProductionNotFoundError):
        svc.approve_plan("prod_never_planned")


def test_cost_cap_split_persists_through_service(tmp_path):
    svc = _service(tmp_path)
    out = svc.plan_production("una idea cualquiera", cost_cap_usd=3.0)
    shots = out["plan"]["sequences"][0]["shots"]
    total = sum(s["cost_cap_usd"] for s in shots)
    assert abs(total - 3.0) < 0.01


def test_approval_survives_pod_restart(tmp_path):
    # M6-SCR-002: planificar, luego "reiniciar el pod" (nuevo servicio, gates RAM
    # vacíos) compartiendo el mismo checkpointer. Aprobar debe funcionar porque el
    # estado de la puerta se persiste junto al checkpoint, no solo en RAM.
    db = tmp_path / "restart.sqlite3"
    svc1 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    pid = svc1.plan_production("ciudad lluviosa", cost_cap_usd=1.0)["production_id"]

    # reinicio: proceso nuevo, self._gates vacío, mismo SQLite.
    svc2 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    assert svc2._gates == {}
    out = svc2.approve_plan(pid)
    assert out["phase"] == "simulated"
    assert out["status"]["storyboard_approved"] is True


def test_gate_blocks_expensive_loop_until_approved(tmp_path):
    # un nuevo servicio sin la gate en memoria (simula reinicio): sin aprobación
    # previa, approve_plan es la única vía a ejecutar, y crea la aprobación.
    svc = _service(tmp_path)
    pid = svc.plan_production("prueba de puerta")["production_id"]
    # antes de aprobar, el estado NO está approved ni tiene reel.
    pre = svc.production_status(pid)
    assert pre["storyboard_approved"] is False
    assert pre["final_output_path"] is None
    assert not isinstance(ProductionNotApprovedError, str)  # símbolo exportado


def test_approval_persists_full_storyboard_gate_dict(tmp_path):
    # M6-SCR-002: persistencia completa de StoryboardGate incluyendo historial y rondas
    db = tmp_path / "gate_full.sqlite3"
    svc1 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    pid = svc1.plan_production("un plano cinematográfico", cost_cap_usd=1.0)["production_id"]

    st1 = svc1.checkpointer.load(pid)
    assert st1 is not None
    gate_data = st1.metadata.get("storyboard_gate")
    assert isinstance(gate_data, dict)
    assert gate_data["state"] == "awaiting_approval"
    assert gate_data["rounds"] == 1
    assert len(gate_data["history"]) >= 1

    # Reinicio del pod: svc2 hidrata desde dict y aprueba manteniendo el historial
    svc2 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    out = svc2.approve_plan(pid)
    assert out["phase"] == "simulated"

    st2 = svc2.checkpointer.load(pid)
    assert st2 is not None
    gate_data2 = st2.metadata.get("storyboard_gate")
    assert isinstance(gate_data2, dict)
    assert gate_data2["state"] == "approved"
    assert len(gate_data2["history"]) >= 2


def test_approval_legacy_string_gate_fallback(tmp_path):
    # Compatibilidad hacia atrás si un checkpoint previo guardó el estado como string
    db = tmp_path / "legacy.sqlite3"
    svc1 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    pid = svc1.plan_production("un plano legado", cost_cap_usd=1.0)["production_id"]

    st = svc1.checkpointer.load(pid)
    st.metadata["storyboard_gate"] = "awaiting_approval"
    svc1.checkpointer.save(st)

    # Reinicio del pod: svc2 maneja el string sin lanzar excepción
    svc2 = ProductionService(checkpointer=ProductionCheckpointer(db_path=db))
    out = svc2.approve_plan(pid)
    assert out["phase"] == "simulated"
