"""StudioClient tipado para la UI agéntica (plan Ultra, W21 / A23).

W21 pide una interfaz agéntica apoyada en componentes MIT de OpenCode con un
**StudioClient propio** sobre la API del estudio: conversación, archivos,
previews, timeline, progreso de herramientas, pausas/cancelación, reanudación,
evidencia y controles. El invariante (A23): no simular el SDK ajeno; reconnect
no duplica acciones; un evento de error no deja spinner infinito ni éxito falso;
sin CDN.

Este módulo implementa el CONTRATO del StudioClient (lógica pura, sin red): el
modelo de acciones idempotentes y estados honestos que el frontend usa. El
transporte HTTP/SSE real lo añade la capa web; aquí se verifica la idempotencia
de reconexión y que los estados de error/progreso son honestos (no un spinner
que nunca termina ni un éxito inventado).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class ClientError(RuntimeError):
    """Error del StudioClient."""


@dataclass(slots=True)
class Action:
    """Una acción del cliente con clave de idempotencia (reconnect-safe)."""

    idempotency_key: str
    kind: str                 # "create_project" | "upload" | "send_brief" | ...
    state: ActionState = ActionState.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.state in (ActionState.DONE, ActionState.ERROR, ActionState.CANCELLED)


@dataclass(slots=True)
class StudioClient:
    """Cliente agéntico: acciones idempotentes y estados honestos (A23)."""

    #: acciones por clave de idempotencia (reconnect no duplica).
    _actions: dict[str, Action] = field(default_factory=dict)

    def submit(self, idempotency_key: str, kind: str) -> Action:
        """Envía una acción. Si la clave ya existe, DEVUELVE la existente.

        A23: reconnect no duplica acciones — reenviar la misma clave no crea una
        segunda acción ni re-ejecuta una ya terminal.
        """
        existing = self._actions.get(idempotency_key)
        if existing is not None:
            return existing  # idempotente: misma acción, no se duplica
        action = Action(idempotency_key=idempotency_key, kind=kind, state=ActionState.RUNNING)
        self._actions[idempotency_key] = action
        return action

    def complete(self, idempotency_key: str, result: dict[str, Any]) -> None:
        action = self._require(idempotency_key)
        if action.state is ActionState.CANCELLED:
            return  # una cancelada no se "completa" por una respuesta tardía
        action.state = ActionState.DONE
        action.result = result

    def fail(self, idempotency_key: str, error: str) -> None:
        """Un error deja la acción en ERROR con mensaje — nunca spinner infinito."""
        action = self._require(idempotency_key)
        action.state = ActionState.ERROR
        action.error = error or "error sin detalle"

    def cancel(self, idempotency_key: str) -> None:
        action = self._require(idempotency_key)
        if action.state in (ActionState.DONE, ActionState.ERROR):
            return  # ya terminal; cancelar no revierte un resultado real
        action.state = ActionState.CANCELLED

    def resume(self, idempotency_key: str) -> Action:
        """Reanuda/consulta una acción tras un reconnect (no la re-ejecuta)."""
        return self._require(idempotency_key)

    def ui_status(self, idempotency_key: str) -> dict[str, Any]:
        """Estado para la UI: nunca 'cargando' sin fin ni éxito falso (A23)."""
        action = self._require(idempotency_key)
        spinner = action.state is ActionState.RUNNING
        return {
            "state": str(action.state),
            # el spinner solo mientras corre de verdad; un error lo apaga.
            "show_spinner": spinner,
            "error": action.error,
            "success": action.state is ActionState.DONE,
        }

    def _require(self, key: str) -> Action:
        action = self._actions.get(key)
        if action is None:
            raise ClientError(f"accion desconocida: {key}")
        return action


__all__ = ["Action", "ActionState", "ClientError", "StudioClient"]
