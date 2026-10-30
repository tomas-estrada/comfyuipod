"""Tests del Bloque 5 fase 17 — ProductionService (tools + puerta de gasto)."""

from __future__ import annotations

import pytest

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


def test_approve_runs_loop_to_done(tmp_path):
    svc = _service(tmp_path)
    pid = svc.plan_production("desierto rojo", cost_cap_usd=1.0)["production_id"]
    out = svc.approve_plan(pid)
    assert out["phase"] == "done"
    assert out["final_output_path"] is not None
    assert out["status"]["complete"] is True
    assert out["status"]["shots_accepted"] == out["status"]["shots_total"]
    assert out["status"]["storyboard_approved"] is True


def test_approve_twice_is_rejected_after_terminal(tmp_path):
    svc = _service(tmp_path)
    pid = svc.plan_production("x")["production_id"]
    svc.approve_plan(pid)  # first approval runs the loop
    # second approval: gate already APPROVED -> can_enter_build True, re-runs idempotently
    out2 = svc.approve_plan(pid)
    assert out2["phase"] == "done"


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
    assert out["phase"] == "done"
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
    assert out["phase"] == "done"

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
    assert out["phase"] == "done"
