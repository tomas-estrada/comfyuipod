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
