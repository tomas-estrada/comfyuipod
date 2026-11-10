"""Investigador autónomo y aprendizaje selectivo (plan Ultra, W23 / A26).

Hipótesis preregistrada; Pareto por resultado aceptado; no promoción desde
mocks; hipótesis negativa termina; budget acotado. Puro CPU.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from comfy_studio.researcher import (
    AdapterDataset,
    AdapterProposal,
    Experiment,
    Hypothesis,
    PromotionProposal,
    Registry,
    ResearchError,
    SQLiteResearchStore,
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
                      simulated=kw.get("simulated", False),
                      dataset_sha256s=kw.get("dataset_sha256s", []))


def _adapter_proposal(**kw) -> AdapterProposal:
    defaults = {
        "proposal_id": "adapter-p1",
        "hyp_id": "h1",
        "run_id": "winning-run",
        "name": "style-adapter-v1",
        "kind": "lora",
        "base_model": "model-x",
        "base_model_revision": "b" * 40,
        "code_revision": "a" * 40,
        "datasets": [
            AdapterDataset("train-a", "1" * 64, "CC0-1.0", "train", True, "license-record"),
            AdapterDataset(
                "validation-a", "2" * 64, "CC-BY-4.0", "validation", True, "license-record"
            ),
            AdapterDataset("holdout-a", "3" * 64, "CC0-1.0", "test", True, "license-record"),
        ],
        "hyperparameters": {"rank": 16, "alpha": 16, "learning_rate": 0.0001},
        "rollback_reference": "revert PR and restore adapter manifest v0",
    }
    defaults.update(kw)
    return AdapterProposal(**defaults)


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


def test_experiment_defaults_to_simulated_until_proven_real() -> None:
    experiment = Experiment(run_id="unknown", hyp_id="h1", seed=1, scores={"vbench": 0.9})
    assert experiment.simulated is True


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


def test_direct_mutation_cannot_change_preregistered_criterion() -> None:
    reg = Registry(hypothesis=_hyp())
    reg.add_run(_exp("r1", 0.6))
    reg.hypothesis.criterion = 0.5
    with pytest.raises(ResearchError, match="preregistro está congelado"):
        reg.accepted()


def test_registry_rejects_duplicate_run_ids() -> None:
    reg = Registry(hypothesis=_hyp())
    reg.add_run(_exp("same", 0.8))
    with pytest.raises(ResearchError, match="run_id duplicado"):
        reg.add_run(_exp("same", 0.9))


def test_sqlite_store_persists_preregistration_budget_and_pareto(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchStore(path)
    store.register(_hyp(budget_runs=4))
    store.record(_exp("good-expensive", 0.8, cost=2.0))
    store.record(_exp("good-cheap", 0.9, cost=1.0))
    store.record(_exp("simulated", 1.0, cost=0.1, simulated=True))
    store.record(_exp("constraint-fail", 1.0, cost=0.1, constraints_ok=False))

    restarted = SQLiteResearchStore(path)
    registry = restarted.get_registry("h1")
    assert len(registry.runs) == 4
    assert {run.run_id for run in restarted.pareto("h1")} == {"good-cheap"}
    with pytest.raises(ResearchError, match="budget de corridas agotado"):
        restarted.record(_exp("over-budget", 1.0))
    with pytest.raises(ResearchError, match="ya preregistrada"):
        restarted.register(_hyp(budget_runs=100))


def test_sqlite_store_requires_preregistration_and_serializes_budget_writes(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchStore(path)
    with pytest.raises(ResearchError, match="preregistrarse antes"):
        store.record(_exp("early", 0.8))
    store.register(_hyp(budget_runs=1))

    def record(run_id: str) -> str:
        try:
            SQLiteResearchStore(path).record(_exp(run_id, 0.8))
        except ResearchError:
            return "rejected"
        return "recorded"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(record, ["r1", "r2"]))
    assert sorted(outcomes) == ["recorded", "rejected"]
    assert len(SQLiteResearchStore(path).get_registry("h1").runs) == 1


def test_sqlite_store_detects_changed_preregistration_payload(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchStore(path)
    store.register(_hyp())
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE hypotheses SET payload=? WHERE hyp_id=?",
            ('{"hyp_id":"h1","criterion":0.1}', "h1"),
        )
    with pytest.raises(ResearchError, match="hash no coincide"):
        store.get_registry("h1")


def test_hypothesis_rejects_non_finite_criterion_and_experiment_cost() -> None:
    with pytest.raises(ResearchError, match="finito"):
        _hyp(criterion=float("nan")).validate()
    with pytest.raises(ResearchError, match="no negativo"):
        _exp("bad-cost", 0.8, cost=-1).validate()


def test_adapter_proposal_persists_only_with_accepted_real_holdout_evidence(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchStore(path)
    store.register(_hyp())
    store.record(
        _exp("winning-run", 0.9, dataset_sha256s=["3" * 64])
    )
    proposal = _adapter_proposal()
    store.propose_adapter(proposal)
    reopened = SQLiteResearchStore(path).get_adapter_proposal("adapter-p1")
    assert reopened.to_dict() == proposal.to_dict()
    assert reopened.training_executed is False
    assert reopened.to_dict()["status"] == "proposed"


@pytest.mark.parametrize(
    ("run_options", "error"),
    [
        ({"simulated": True, "dataset_sha256s": ["3" * 64]}, "evidencia no es aceptable"),
        ({"constraints_ok": False, "dataset_sha256s": ["3" * 64]}, "evidencia no es aceptable"),
        ({"vbench": 0.4, "dataset_sha256s": ["3" * 64]}, "evidencia no es aceptable"),
        ({"dataset_sha256s": ["4" * 64]}, "no acredita el holdout"),
    ],
)
def test_adapter_proposal_rejects_unacceptable_or_unmatched_evidence(
    tmp_path, run_options: dict, error: str
) -> None:
    store = SQLiteResearchStore(tmp_path / "research.sqlite3")
    store.register(_hyp())
    values = {"run_id": "winning-run", **run_options}
    score = values.pop("vbench", 0.9)
    store.record(_exp(vbench=score, **values))
    with pytest.raises(ResearchError, match=error):
        store.propose_adapter(_adapter_proposal())


def test_adapter_proposal_requires_authorized_disjoint_splits_and_no_training() -> None:
    with pytest.raises(ResearchError, match="autorización"):
        _adapter_proposal(datasets=[
            AdapterDataset("train", "1" * 64, "CC0", "train", False, "license record"),
            AdapterDataset("valid", "2" * 64, "CC0", "validation", True, "license record"),
            AdapterDataset("test", "3" * 64, "CC0", "test", True, "license record"),
        ]).validate()
    with pytest.raises(ResearchError, match="filtradas"):
        _adapter_proposal(datasets=[
            AdapterDataset("train", "1" * 64, "CC0", "train", True, "license record"),
            AdapterDataset("valid", "2" * 64, "CC0", "validation", True, "license record"),
            AdapterDataset("test", "1" * 64, "CC0", "test", True, "license record"),
        ]).validate()
    with pytest.raises(ResearchError, match="training"):
        _adapter_proposal(training_executed=True).validate()


def test_adapter_proposal_detects_tampered_persistent_payload(tmp_path) -> None:
    path = tmp_path / "research.sqlite3"
    store = SQLiteResearchStore(path)
    store.register(_hyp())
    store.record(_exp("winning-run", 0.9, dataset_sha256s=["3" * 64]))
    store.propose_adapter(_adapter_proposal())
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE adapter_proposals SET payload=? WHERE proposal_id=?",
            ("{}", "adapter-p1"),
        )
    with pytest.raises(ResearchError, match="propuesta corrupta"):
        SQLiteResearchStore(path).get_adapter_proposal("adapter-p1")
