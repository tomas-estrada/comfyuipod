"""Investigador autónomo y aprendizaje selectivo (plan Ultra, W23 / A26).

Hipótesis preregistrada; Pareto por resultado aceptado; no promoción desde
mocks; hipótesis negativa termina; budget acotado. Puro CPU.
"""

from __future__ import annotations

import pytest

from comfy_studio.researcher import (
    Experiment,
    Hypothesis,
    PromotionProposal,
    Registry,
    ResearchError,
    can_promote,
    pareto_front,
)


def _hyp(**kw) -> Hypothesis:
    defaults = {
        "hyp_id": "h1",
        "intervention": "lightning_lora",
        "metric": "vbench",
        "higher_is_better": True,
        "criterion": 0.7,
        "budget_runs": 3,
    }
    defaults.update(kw)
    return Hypothesis(**defaults)


def _exp(run_id: str, vbench: float, **kw) -> Experiment:
    return Experiment(run_id=run_id, hyp_id="h1", seed=kw.get("seed", 1),
                      scores={"vbench": vbench}, cost=kw.get("cost", 1.0),
                      constraints_ok=kw.get("constraints_ok", True),
                      simulated=kw.get("simulated", False))


def test_criterion_frozen_after_first_result() -> None:
    """A26: criterio fijado antes de resultados; no se ajusta despues."""
    reg = Registry(hypothesis=_hyp())
    reg.add_run(_exp("r1", 0.6))  # primer resultado congela el criterio
    with pytest.raises(ResearchError, match="congelado"):
        reg.hypothesis.set_criterion(0.5)  # intentar bajar el umbral


def test_budget_caps_runs_negative_hypothesis_terminates() -> None:
    """A26: una hipotesis negativa termina, no busca una muestra bonita."""
    reg = Registry(hypothesis=_hyp(budget_runs=2))
    reg.add_run(_exp("r1", 0.5))
    reg.add_run(_exp("r2", 0.55))
    with pytest.raises(ResearchError, match="budget de corridas agotado"):
        reg.add_run(_exp("r3", 0.9))


def test_accepted_requires_real_criterion_and_constraints() -> None:
    reg = Registry(hypothesis=_hyp(criterion=0.7))
    reg.add_run(_exp("ok", 0.8))                      # cumple
    reg.add_run(_exp("low", 0.6))                     # no cumple criterio
    reg.add_run(_exp("bad", 0.9, constraints_ok=False))  # viola restricciones
    accepted = reg.accepted()
    assert [e.run_id for e in accepted] == ["ok"]


def test_simulated_runs_never_accepted() -> None:
    reg = Registry(hypothesis=_hyp())
    reg.add_run(_exp("sim", 0.95, simulated=True))
    assert reg.accepted() == []  # un simulado no se acepta ni promueve


def test_pareto_front_respects_constraints_as_hard() -> None:
    """A26: Pareto con identidad/factualidad como restriccion, no solo score."""
    exps = [
        _exp("a", 0.8, cost=2.0),
        _exp("b", 0.9, cost=1.0),                  # domina a 'a'
        _exp("c", 0.99, cost=0.5, constraints_ok=False),  # mejor score pero viola
    ]
    front = pareto_front(exps, metric="vbench", higher_is_better=True)
    ids = {e.run_id for e in front}
    assert "b" in ids
    assert "c" not in ids  # excluida por restriccion, pese a mejor score


def test_cannot_promote_from_simulated() -> None:
    """A26: no promocion desde mocks."""
    prop = PromotionProposal(
        name="lora_v1", kind="lora", datasets=["d"], licenses=["cc0"],
        hyperparameters={"lr": 1e-4}, holdout="split_test", rollback="revert PR",
    )
    sim = _exp("sim", 0.95, simulated=True)
    with pytest.raises(ResearchError, match="simulada"):
        can_promote(prop, sim, _hyp())


def test_promotion_requires_rollback_and_holdout() -> None:
    real = _exp("real", 0.9)
    no_rollback = PromotionProposal(
        name="x", kind="preset", datasets=[], licenses=[],
        hyperparameters={}, holdout="split", rollback="",
    )
    with pytest.raises(ResearchError, match="rollback"):
        can_promote(no_rollback, real, _hyp())


def test_valid_real_promotion_passes() -> None:
    prop = PromotionProposal(
        name="lora_v1", kind="lora", datasets=["d"], licenses=["cc0"],
        hyperparameters={"lr": 1e-4}, holdout="split_test", rollback="revert PR",
    )
    real = _exp("real", 0.9)  # cumple criterio 0.7, real, constraints ok
    assert can_promote(prop, real, _hyp()) is True
