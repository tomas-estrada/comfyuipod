"""Director autónomo de producción (plan Ultra, W22 / A24).

Flujo brief->constraints->plan->execute->qa->repair->delivery con presupuesto
persistente, constraints conservadas, capacidad informada antes de gastar. CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.director import (
    BudgetExhausted,
    CapabilityUnavailable,
    Constraints,
    DirectorError,
    DirectorState,
    Phase,
    advance,
    block,
    require_capability,
)


def _state(**kw) -> DirectorState:
    return DirectorState(mission_id="m1", **kw)


def test_full_phase_flow_reaches_done() -> None:
    st = _state()
    for _ in range(len(("brief", "constraints", "plan", "execute", "qa", "repair", "delivery"))):
        advance(st, reason="paso")
    assert st.phase is Phase.DONE
    # cada salto registrado con razón.
    assert all("reason" in e for e in st.log if e["event"] == "advance")


def test_fixed_constraints_preserved_even_if_agent_prefers_another() -> None:
    """A24: restricciones fijas de lugar/producto se conservan."""
    c = Constraints(product="tenis", place="puente_4sur", aspect="9:16")
    c.freeze()
    # El agente intenta otro lugar: debe fallar.
    with pytest.raises(DirectorError, match="restriccion de lugar"):
        c.require_preserved(chosen_place="otro_lugar", chosen_product="tenis")
    # Con el lugar y producto correctos, pasa.
    c.require_preserved(chosen_place="puente_4sur", chosen_product="tenis")


def test_product_constraint_preserved() -> None:
    c = Constraints(product="tenis")
    with pytest.raises(DirectorError, match="restriccion de producto"):
        c.require_preserved(chosen_place=None, chosen_product="gaseosa")


def test_budget_limits_steps() -> None:
    st = _state()
    st.budget.max_steps = 2
    advance(st, reason="1")
    advance(st, reason="2")
    with pytest.raises(BudgetExhausted, match="pasos agotado"):
        advance(st, reason="3")


def test_retries_per_hypothesis_bounded() -> None:
    """A24: cada reintento tiene contador y presupuesto."""
    st = _state()
    st.budget.max_retries_per_hypothesis = 2
    assert st.budget.retry("seed_bump") == 1
    assert st.budget.retry("seed_bump") == 2
    with pytest.raises(BudgetExhausted, match="reintentos"):
        st.budget.retry("seed_bump")


def test_capability_missing_informed_before_spending() -> None:
    """A24: una ausencia de capacidad se informa ANTES de gastar."""
    st = _state()
    with pytest.raises(CapabilityUnavailable, match="antes de gastar"):
        require_capability("video.text_to_video", available=False, state=st)
    # Se registró, y no se gastó ningún paso.
    assert st.budget.steps_used == 0
    assert any(e["event"] == "capability_missing" for e in st.log)


def test_available_capability_does_not_block() -> None:
    st = _state()
    require_capability("video.text_to_video", available=True, state=st)  # no lanza


def test_block_is_honest_terminal_not_fake_done() -> None:
    """A24: ruta sin salida se detiene honestamente, no 'done' ficticio."""
    st = _state()
    block(st, reason="pesos gated no disponibles")
    assert st.phase is Phase.BLOCKED
    assert st.phase is not Phase.DONE
    assert any(e["event"] == "blocked" for e in st.log)


def test_state_serializable_with_counters() -> None:
    st = _state()
    advance(st, reason="ir")
    st.budget.retry("h1")
    d = st.to_dict()
    assert d["steps_used"] == 1
    assert d["retries"]["h1"] == 1
    assert d["phase"] == "constraints"
