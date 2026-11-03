"""Servicio de producción (M6 Bloque 5, fase 17).

La superficie que el agente y la API usan para conducir una producción de punta a
punta, atando las piezas del Bloque 4 (grafo + checkpointer + planner/executor/
evaluator/assembler) con la `StoryboardGate` existente (M4): el borrador barato se
planifica libremente, pero el lazo caro (ejecución + ensamblaje) NO corre hasta que
el humano aprueba. Es la "puerta de gasto" del plan maestro, sin cambios de M4.

Expone cuatro operaciones (los tools del agente de la fase 17):
  - `plan_production(idea, cost_cap)` → planifica (barato) y deja la producción en
    AWAITING_APPROVAL; devuelve el plan para que el humano lo revise.
  - `get_plan(production_id)` → el plan actual (estado serializado).
  - `approve_plan(production_id)` → el humano aprueba; corre el lazo caro
    (ejecución+evaluación+replan+ensamblaje) y devuelve el resultado.
  - `production_status(production_id)` → fase, progreso de shots, reel final.

Criterio: servicio PURO (tipado, sin FastAPI dentro). La ruta HTTP y los tools MCP
se cablean en el server (como el resto). Los colaboradores reales se inyectan vía
`Collaborators`; por defecto stubs → corre en CI sin GPU/LLM.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.engines.storyboard_gate import StoryboardGate, StoryboardState
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.conductor import Collaborators, build_production_graph
from comfy_studio.orchestration.planner import plan_production as _plan
from comfy_studio.orchestration.state import ProductionState, ShotStatus


class ProductionNotFoundError(RuntimeError):
    """No existe un checkpoint para el production_id pedido."""


class ProductionNotApprovedError(RuntimeError):
    """Se intentó correr el lazo caro sin aprobación del storyboard."""


def _gate_key(production_id: str) -> str:
    return f"__gate__{production_id}"


class ProductionService:
    """Conduce producciones atando grafo + checkpointer + StoryboardGate."""

    def __init__(
        self,
        *,
        checkpointer: ProductionCheckpointer | None = None,
        collaborators: Collaborators | None = None,
        store: Any | None = None,
    ) -> None:
        self.checkpointer = checkpointer or ProductionCheckpointer()
        self.collaborators = collaborators or Collaborators()
        # Proyecto de la galería donde se registran las salidas (None = sin
        # registrar; CI/stubs). Los renders de una producción aprobada pasan a
        # ser assets de primera clase: buscables, descargables, vision_check.
        self.store = store
        # puertas de storyboard por producción (serializadas junto al estado).
        self._gates: dict[str, StoryboardGate] = {}

    # -- tool: plan_production ----------------------------------------------

    def plan_production(
        self,
        idea: str,
        *,
        cost_cap_usd: float = 0.0,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Planifica (barato) y deja la producción esperando aprobación humana."""
        state = ProductionState(idea=idea, cost_cap_usd=cost_cap_usd)
        if project_id:
            # fail-closed: un proyecto inexistente se rechaza al planificar,
            # no al aprobar (cuando ya se gastó la aprobación humana).
            if self.store is not None and self.store.get_project(project_id) is None:
                raise LookupError(f"Proyecto {project_id!r} no encontrado")
            state.metadata["project_id"] = project_id
        _plan(state, self.collaborators.planner)
        # el plan es el "borrador barato": se somete a la puerta.
        gate = StoryboardGate()
        gate.submit_draft(draft_path=f"plan:{state.id}")
        self._gates[state.id] = gate
        # PERSISTIR la puerta completa junto al checkpoint: si no, un reinicio
        # del pod vacía self._gates y toda producción planificada queda bloqueada
        # para siempre al aprobar, perdiendo historial y rondas (M6-SCR-002).
        state.metadata["storyboard_gate"] = gate.to_dict()
        state.phase = "awaiting_approval"
        state.record("await_approval", shots=len(state.all_shots()))
        self.checkpointer.save(state)
        return {
            "production_id": state.id,
            "phase": state.phase,
            "storyboard_state": str(gate.state),
            "plan": state.to_dict(),
        }

    # -- tool: get_plan ------------------------------------------------------

    def get_plan(self, production_id: str) -> dict[str, Any]:
        state = self._load(production_id)
        return {"production_id": production_id, "plan": state.to_dict()}

    # -- tool: approve_plan --------------------------------------------------

    def approve_plan(self, production_id: str) -> dict[str, Any]:
        """El humano aprueba; corre el lazo caro y devuelve el resultado."""
        state = self._load(production_id)
        # hidratar la puerta: primero la de RAM; si el pod reinició, desde el
        # estado persistido en el checkpoint (M6-SCR-002).
        gate = self._gates.get(production_id)
        if gate is None:
            persisted = state.metadata.get("storyboard_gate")
            if isinstance(persisted, dict):
                gate = StoryboardGate.from_dict(persisted)
            elif isinstance(persisted, str):
                gate = StoryboardGate(state=StoryboardState(persisted))
            else:
                gate = StoryboardGate()
        # avanzar la puerta a APPROVED (fail-closed si no estaba esperando).
        if not gate.can_enter_build():
            if gate.state is StoryboardState.AWAITING_APPROVAL:
                gate.approve()
            else:
                raise ProductionNotApprovedError(
                    f"storyboard no aprobable desde {gate.state}"
                )
        self._gates[production_id] = gate
        state.metadata["storyboard_gate"] = gate.to_dict()
        state.storyboard_approved = True
        state.phase = "executing"
        state.record("approved")

        # correr el lazo caro (ejecución + evaluación + replan + ensamblaje).
        graph = build_production_graph(
            checkpointer=self.checkpointer,
            collaborators=self.collaborators,
        )
        final = graph.run(state)
        registered = self._register_outputs_in_gallery(final)
        self.checkpointer.save(final)
        return {
            "production_id": production_id,
            "phase": final.phase,
            "final_output_path": final.final_output_path,
            "output_asset_ids": registered,
            "status": self._status_dict(final),
        }

    # -- tool: production_status --------------------------------------------

    def production_status(self, production_id: str) -> dict[str, Any]:
        state = self._load(production_id)
        return self._status_dict(state)

    # -- helpers -------------------------------------------------------------

    def _register_outputs_in_gallery(self, state: ProductionState) -> list[str]:
        """Registra las salidas de la producción como assets de la galería.

        Cada shot aceptado y el reel final (si existen físicamente) pasan a ser
        assets del proyecto con linaje completo (production_id, shot_id,
        secuencia). Tolerante: sin store, archivo inexistente o fallo de
        registro, se omite ESE asset y se registra el evento — la producción
        nunca se cae por el registro de galería.
        """
        project_id = state.metadata.get("project_id")
        if not project_id or self.store is None:
            return []
        from pathlib import Path

        registered: list[str] = []
        for shot in state.all_shots():
            if shot.status is not ShotStatus.ACCEPTED or not shot.output_path:
                continue
            path = Path(shot.output_path)
            if not path.is_file():
                state.record(
                    "gallery_skip", shot=shot.id, reason="output inexistente"
                )
                continue
            try:
                asset = self.store.save_upload(
                    project_id,
                    filename=f"prod_{state.id[:8]}_{shot.id[:12]}.mp4",
                    content_type="video/mp4",
                    data=path.read_bytes(),
                    max_upload_bytes=2 * 1024 * 1024 * 1024,
                    category="outputs",
                    metadata={
                        "production_render": True,
                        "production_id": state.id,
                        "shot_id": shot.id,
                        "shot_description": shot.description,
                        "production_phase": str(state.phase),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                state.record(
                    "gallery_error", shot=shot.id, error=str(exc)[:300]
                )
                continue
            registered.append(str(asset["id"]))
        # reel final
        if state.final_output_path:
            reel = Path(state.final_output_path)
            if reel.is_file():
                try:
                    asset = self.store.save_upload(
                        project_id,
                        filename=f"prod_{state.id[:8]}_reel.mp4",
                        content_type="video/mp4",
                        data=reel.read_bytes(),
                        max_upload_bytes=2 * 1024 * 1024 * 1024,
                        category="outputs",
                        metadata={
                            "production_render": True,
                            "production_id": state.id,
                            "is_final_reel": True,
                            "production_phase": str(state.phase),
                        },
                    )
                    registered.append(str(asset["id"]))
                except Exception as exc:  # noqa: BLE001
                    state.record("gallery_error", error=str(exc)[:300])
        state.metadata["output_asset_ids"] = registered
        if registered:
            state.record("gallery_registered", assets=len(registered))
        return registered

    def _load(self, production_id: str) -> ProductionState:
        state = self.checkpointer.load(production_id)
        if state is None:
            raise ProductionNotFoundError(production_id)
        return state

    @staticmethod
    def _status_dict(state: ProductionState) -> dict[str, Any]:
        shots = state.all_shots()
        by_status: dict[str, int] = {}
        for s in shots:
            by_status[str(s.status)] = by_status.get(str(s.status), 0) + 1
        accepted = sum(1 for s in shots if s.status == ShotStatus.ACCEPTED)
        return {
            "production_id": state.id,
            "phase": state.phase,
            "idea": state.idea,
            "project_id": state.metadata.get("project_id"),
            "shots_total": len(shots),
            "shots_accepted": accepted,
            "shots_by_status": by_status,
            "storyboard_approved": state.storyboard_approved,
            "final_output_path": state.final_output_path,
            "output_asset_ids": state.metadata.get("output_asset_ids") or [],
            "complete": state.is_complete(),
        }
