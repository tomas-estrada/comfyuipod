"""Puerta de aprobación del storyboard (M4-SCR, F3).

La pieza que hace ASEQUIBLE el CGI pesado: el agente produce un borrador RÁPIDO
(modelos simples, baja resolución) y el pipeline NO entra en la fase cara
(BUILD→POST con Blender + vídeo pesado) hasta que el humano aprueba. Solo pagas
GPU pesada sobre una idea ya aprobada.

Es una máquina de estados pura, sin acoplar a ningún motor, para poder probarla
sin GPU. El pipeline la consulta antes de gastar.

Estados:

    DRAFTING ──submit_draft──▶ AWAITING_APPROVAL ──approve──▶ APPROVED
                                     │  │
                              reject │  └── request_changes ──▶ DRAFTING
                                     ▼
                                 REJECTED

Reglas:
  - `can_enter_build()` es True SOLO en APPROVED. Cualquier otra cosa bloquea.
  - `reject` es terminal (la idea se descarta). `request_changes` vuelve a
    DRAFTING para otra vuelta de borrador barato.
  - Toda transición inválida lanza `GateTransitionError` (fail-closed): nunca se
    "cae" a BUILD por un estado ambiguo.
  - Se serializa a dict para persistir en el proyecto (SQLite) y reanudar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class StoryboardState(StrEnum):
    DRAFTING = "drafting"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


class GateTransitionError(RuntimeError):
    """Transición de estado inválida en la puerta del storyboard."""


@dataclass(slots=True)
class StoryboardGate:
    """Máquina de estados de la aprobación del storyboard de un proyecto."""

    state: StoryboardState = StoryboardState.DRAFTING
    draft_path: str | None = None
    rounds: int = 0  # cuántos borradores se han enviado
    history: list[dict[str, Any]] = field(default_factory=list)

    # -- transiciones --------------------------------------------------------

    def submit_draft(self, draft_path: str) -> None:
        """El agente entrega un borrador rápido para revisión humana."""
        if self.state not in (StoryboardState.DRAFTING,):
            raise GateTransitionError(
                f"submit_draft solo válido en DRAFTING, no en {self.state}"
            )
        self.draft_path = draft_path
        self.rounds += 1
        self._to(StoryboardState.AWAITING_APPROVAL, {"draft_path": draft_path})

    def approve(self) -> None:
        """El humano aprueba: el pipeline puede entrar en BUILD caro."""
        if self.state is not StoryboardState.AWAITING_APPROVAL:
            raise GateTransitionError(
                f"approve solo válido en AWAITING_APPROVAL, no en {self.state}"
            )
        self._to(StoryboardState.APPROVED, {})

    def reject(self, reason: str = "") -> None:
        """El humano descarta la idea (terminal)."""
        if self.state is not StoryboardState.AWAITING_APPROVAL:
            raise GateTransitionError(
                f"reject solo válido en AWAITING_APPROVAL, no en {self.state}"
            )
        self._to(StoryboardState.REJECTED, {"reason": reason})

    def request_changes(self, note: str = "") -> None:
        """El humano pide otra vuelta de borrador barato."""
        if self.state is not StoryboardState.AWAITING_APPROVAL:
            raise GateTransitionError(
                f"request_changes solo válido en AWAITING_APPROVAL, no en {self.state}"
            )
        self._to(StoryboardState.DRAFTING, {"note": note})

    # -- consultas -----------------------------------------------------------

    def can_enter_build(self) -> bool:
        """El pipeline consulta esto antes de gastar GPU pesada."""
        return self.state is StoryboardState.APPROVED

    def is_terminal(self) -> bool:
        return self.state in (StoryboardState.APPROVED, StoryboardState.REJECTED)

    # -- persistencia --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": str(self.state),
            "draft_path": self.draft_path,
            "rounds": self.rounds,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoryboardGate:
        return cls(
            state=StoryboardState(data.get("state", "drafting")),
            draft_path=data.get("draft_path"),
            rounds=int(data.get("rounds", 0)),
            history=list(data.get("history", [])),
        )

    # -- interno -------------------------------------------------------------

    def _to(self, new: StoryboardState, meta: dict[str, Any]) -> None:
        self.history.append({"from": str(self.state), "to": str(new), **meta})
        self.state = new
