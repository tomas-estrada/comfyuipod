"""Director autónomo de producción (plan Ultra, W22 / A24).

W22 integra la cadena en una misión autónoma:
`prompt+assets → brief → constraints → planes → engines → QA → repair → delivery`,
con presupuestos persistentes, escalado multi-fidelidad y uso de memoria. El
invariante (A24): restricciones fijas de producto/lugar se conservan aunque el
agente prefiera otra idea; cada salto/reintento tiene razón, contador y
presupuesto; una ausencia de capacidad se informa ANTES de gastar; responder
"viral" no acredita éxito social.

Este módulo es el CONTRATO del director: una máquina de fases que avanza una
producción, mantiene un presupuesto persistente, conserva constraints
inmutables, y comprueba la disponibilidad de capacidad antes de gastar. No
ejecuta GPU: delega en las piezas ya construidas (planner/bind_recipes/executor/
lease) y en los engines reales en el pod; aquí se verifica el flujo, el
presupuesto y la conservación de restricciones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    BRIEF = "brief"
    CONSTRAINTS = "constraints"
    PLAN = "plan"
    EXECUTE = "execute"
    QA = "qa"
    REPAIR = "repair"
    DELIVERY = "delivery"
    DONE = "done"
    BLOCKED = "blocked"


#: orden de avance de fases del director.
PHASE_ORDER: tuple[Phase, ...] = (
    Phase.BRIEF,
    Phase.CONSTRAINTS,
    Phase.PLAN,
    Phase.EXECUTE,
    Phase.QA,
    Phase.REPAIR,
    Phase.DELIVERY,
    Phase.DONE,
)


class DirectorError(RuntimeError):
    """Error del flujo del director."""


class BudgetExhausted(DirectorError):
    """El presupuesto de la misión se agotó."""


class CapabilityUnavailable(DirectorError):
    """Una capacidad requerida no está disponible (se informa antes de gastar)."""


@dataclass(slots=True)
class Constraints:
    """Restricciones FIJAS de producto/lugar que el director debe conservar.

    A24: se conservan incluso si el agente prefiere otra idea. Son inmutables
    una vez fijadas por la misión (el director no las reescribe).
    """

    product: str | None = None
    place: str | None = None
    aspect: str = "16:9"
    _frozen: bool = False

    def freeze(self) -> None:
        self._frozen = True

    def require_preserved(self, chosen_place: str | None, chosen_product: str | None) -> None:
        """Falla si una elección del agente viola una restricción fija (A24)."""
        if self.place and chosen_place and chosen_place != self.place:
            raise DirectorError(
                f"restriccion de lugar violada: fijado {self.place!r}, elegido {chosen_place!r}"
            )
        if self.product and chosen_product and chosen_product != self.product:
            raise DirectorError(
                f"restriccion de producto violada: fijado {self.product!r}, "
                f"elegido {chosen_product!r}"
            )


@dataclass(slots=True)
class Budget:
    """Presupuesto persistente de la misión (pasos y reintentos acotados)."""

    max_steps: int = 50
    max_retries_per_hypothesis: int = 3
    steps_used: int = 0
    retries: dict[str, int] = field(default_factory=dict)

    def spend_step(self, reason: str) -> None:
        if self.steps_used >= self.max_steps:
            raise BudgetExhausted(f"presupuesto de pasos agotado ({self.max_steps}): {reason}")
        self.steps_used += 1

    def retry(self, hypothesis: str) -> int:
        """Registra un reintento; falla al exceder el máximo por hipótesis."""
        n = self.retries.get(hypothesis, 0) + 1
        if n > self.max_retries_per_hypothesis:
            raise BudgetExhausted(
                f"hipotesis {hypothesis!r} agoto sus {self.max_retries_per_hypothesis} reintentos"
            )
        self.retries[hypothesis] = n
        return n


@dataclass(slots=True)
class DirectorState:
    """Estado de la misión del director: fase, presupuesto, constraints, log."""

    mission_id: str
    phase: Phase = Phase.BRIEF
    budget: Budget = field(default_factory=Budget)
    constraints: Constraints = field(default_factory=Constraints)
    log: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event: str, **fields: Any) -> None:
        """Cada salto/reintento tiene razón y contador (A24)."""
        self.log.append({"phase": str(self.phase), "event": event, **fields})

    def to_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "phase": str(self.phase),
            "steps_used": self.budget.steps_used,
            "retries": dict(self.budget.retries),
            "constraints": {
                "product": self.constraints.product,
                "place": self.constraints.place,
                "aspect": self.constraints.aspect,
            },
            "log": list(self.log),
        }


def require_capability(name: str, available: bool, state: DirectorState) -> None:
    """Informa una ausencia de capacidad ANTES de gastar (A24).

    No se avanza a ejecución si una capacidad obligatoria falta: se registra y se
    lanza, en vez de gastar presupuesto para fallar después.
    """
    if not available:
        state.record("capability_missing", capability=name)
        raise CapabilityUnavailable(
            f"capacidad '{name}' no disponible: se informa antes de gastar"
        )


def advance(state: DirectorState, *, reason: str) -> Phase:
    """Avanza a la siguiente fase, gastando un paso del presupuesto con razón."""
    if state.phase in (Phase.DONE, Phase.BLOCKED):
        return state.phase
    state.budget.spend_step(reason)
    idx = PHASE_ORDER.index(state.phase)
    state.phase = PHASE_ORDER[idx + 1]
    state.record("advance", to=str(state.phase), reason=reason)
    return state.phase


def block(state: DirectorState, *, reason: str) -> None:
    """Detiene la misión honestamente con un motivo (no un 'done' ficticio)."""
    state.phase = Phase.BLOCKED
    state.record("blocked", reason=reason)


__all__ = [
    "PHASE_ORDER",
    "Budget",
    "BudgetExhausted",
    "CapabilityUnavailable",
    "Constraints",
    "DirectorError",
    "DirectorState",
    "Phase",
    "advance",
    "block",
    "require_capability",
]
