"""Investigador autónomo y aprendizaje selectivo (plan Ultra, W23 / A26).

W23 pide un investigador que formule hipótesis medibles, compare recetas con
holdouts/controles, acumule una frontera Pareto y solo promueva cambios con
evidencia. El invariante (A26): hipótesis/intervención/métrica/criterio/budget
fijados ANTES de resultados; Pareto por resultado aceptado; una propuesta LoRA
incluye datos/licencias/holdout/rollback y el training NO se ejecuta ahora; no
promoción desde mocks ni reset de budget; una hipótesis negativa termina.

Este módulo implementa el CONTRATO:

- `Hypothesis`: preregistro (intervención, métrica, criterio, budget) fijado
  antes de ver resultados. No se puede alterar el criterio tras los resultados.
- `Experiment`: una corrida con sus medidas (seeds/escenas), marca `simulated`.
- `pareto_front`: frontera de Pareto sobre resultados ACEPTADOS (no simulados),
  con identidad/factualidad como restricciones duras, no solo score.
- `can_promote`: solo promueve una propuesta (LoRA/preset) con evidencia real
  (no simulada) que cumple el criterio preregistrado; exige rollback declarado.

Puro cálculo determinista; no entrena nada (A26: training no se ejecuta ahora).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ResearchError(ValueError):
    """Contrato de investigación inválido."""


@dataclass(slots=True)
class Hypothesis:
    """Preregistro: fijado ANTES de ver resultados (A26)."""

    hyp_id: str
    intervention: str
    metric: str          # p. ej. "vbench", "clip_sim"
    higher_is_better: bool
    criterion: float     # umbral que la métrica debe cruzar
    budget_runs: int     # nº máximo de corridas antes de concluir
    #: una vez registrada con resultados, el criterio queda CONGELADO.
    _locked: bool = False

    def lock(self) -> None:
        self._locked = True

    def set_criterion(self, value: float) -> None:
        if self._locked:
            raise ResearchError(
                f"{self.hyp_id}: el criterio esta congelado; no se ajusta tras ver resultados"
            )
        self.criterion = value


@dataclass(slots=True)
class Experiment:
    """Una corrida con sus medidas; marca si es simulada (no promovible)."""

    run_id: str
    hyp_id: str
    seed: int
    scores: dict[str, float]          # métrica -> valor
    constraints_ok: bool = True        # identidad/factualidad respetadas
    cost: float = 0.0
    simulated: bool = False            # un simulado NUNCA promueve (A26)

    def meets(self, hyp: Hypothesis) -> bool:
        val = self.scores.get(hyp.metric)
        if val is None:
            return False
        return val >= hyp.criterion if hyp.higher_is_better else val <= hyp.criterion


@dataclass(slots=True)
class Registry:
    """Registro de experimentos de una hipótesis, con budget acotado."""

    hypothesis: Hypothesis
    runs: list[Experiment] = field(default_factory=list)

    def add_run(self, exp: Experiment) -> None:
        if exp.hyp_id != self.hypothesis.hyp_id:
            raise ResearchError("experimento de otra hipotesis")
        if len(self.runs) >= self.hypothesis.budget_runs:
            raise ResearchError(
                f"budget de corridas agotado ({self.hypothesis.budget_runs}); "
                "una hipotesis negativa termina, no busca una muestra bonita"
            )
        self.runs.append(exp)
        self.hypothesis.lock()  # al primer resultado, el criterio se congela

    def real_runs(self) -> list[Experiment]:
        return [r for r in self.runs if not r.simulated]

    def accepted(self) -> list[Experiment]:
        """Corridas reales que cumplen criterio Y respetan las restricciones."""
        return [
            r for r in self.real_runs()
            if r.constraints_ok and r.meets(self.hypothesis)
        ]


def pareto_front(
    experiments: list[Experiment], *, metric: str, higher_is_better: bool
) -> list[Experiment]:
    """Frontera de Pareto (métrica vs. coste) sobre corridas con restricciones ok.

    Solo entran corridas REALES con `constraints_ok` (identidad/factualidad como
    restricción dura, no solo score). Menor coste y mejor métrica dominan.
    """
    pool = [e for e in experiments if not e.simulated and e.constraints_ok and metric in e.scores]
    front: list[Experiment] = []
    for cand in pool:
        dominated = False
        for other in pool:
            if other is cand:
                continue
            better_metric = (
                other.scores[metric] >= cand.scores[metric]
                if higher_is_better
                else other.scores[metric] <= cand.scores[metric]
            )
            better_cost = other.cost <= cand.cost
            strictly = (
                (other.scores[metric] != cand.scores[metric]) or (other.cost != cand.cost)
            )
            if better_metric and better_cost and strictly:
                dominated = True
                break
        if not dominated:
            front.append(cand)
    return front


@dataclass(slots=True)
class PromotionProposal:
    """Propuesta de promoción (LoRA/preset) con rollback declarado (A26)."""

    name: str
    kind: str              # "lora" | "preset"
    datasets: list[str]
    licenses: list[str]
    hyperparameters: dict[str, Any]
    holdout: str
    rollback: str          # cómo revertir si falla en producción


def can_promote(proposal: PromotionProposal, winning: Experiment, hyp: Hypothesis) -> bool:
    """¿Se puede promover? Solo con evidencia REAL que cumple el criterio (A26)."""
    if winning.simulated:
        raise ResearchError("no se promueve desde una corrida simulada (A26)")
    if not winning.constraints_ok:
        raise ResearchError("la corrida ganadora viola identidad/factualidad")
    if not proposal.rollback:
        raise ResearchError("la propuesta debe declarar un rollback")
    if not proposal.holdout:
        raise ResearchError("la propuesta debe declarar un holdout")
    return winning.meets(hyp)


__all__ = [
    "Experiment",
    "Hypothesis",
    "PromotionProposal",
    "Registry",
    "ResearchError",
    "can_promote",
    "pareto_front",
]
