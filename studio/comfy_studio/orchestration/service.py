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
from comfy_studio.orchestration.recipe_binding import bind_recipes
from comfy_studio.orchestration.state import ProductionState, ShotStatus
from comfy_studio.progress import ProgressBroker


class ProductionNotFoundError(RuntimeError):
    """No existe un checkpoint para el production_id pedido."""


class ProductionNotApprovedError(RuntimeError):
    """Se intentó correr el lazo caro sin aprobación del storyboard."""


class ProductionPlanNotEditableError(RuntimeError):
    """El plan solo es editable mientras la producción espera aprobación."""


class ProductionAlreadyTerminalError(RuntimeError):
    """La producción ya terminó (done/simulated/failed/cancelled/halted)."""


#: fases terminales: no admiten edición de plan ni cancelación.
_TERMINAL_PHASES = frozenset({"done", "simulated", "failed", "cancelled", "halted"})


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
        broker: ProgressBroker | None = None,
        atlas_service: Any | None = None,
        resource_vram_budget_mb: int | None = None,
    ) -> None:
        self.checkpointer = checkpointer or ProductionCheckpointer()
        self.collaborators = collaborators or Collaborators()
        self.resource_vram_budget_mb = resource_vram_budget_mb
        self._fully_wired = all(
            getattr(self.collaborators, name) is not None
            for name in (
                "planner", "runner", "evaluator", "reselect_template",
                "replan_shot", "assembler",
            )
        )
        # Proyecto de la galería donde se registran las salidas (None = sin
        # registrar; CI/stubs). Los renders de una producción aprobada pasan a
        # ser assets de primera clase: buscables, descargables, vision_check.
        self.store = store
        # Broker SSE opcional para eventos de ciclo de vida y progreso por paso.
        self.broker = broker
        # puertas de storyboard por producción (serializadas junto al estado).
        self._gates: dict[str, StoryboardGate] = {}
        # cancelaciones cooperativadas en proceso (M7-AGT-001): el grafo en vuelo
        # corre sobre su propia copia del estado y no ve el flag persistido por
        # `cancel_production`; este set es la vía que sí llega al lazo vía
        # `graph.should_cancel`.
        self._cancel_requested: set[str] = set()
        # Atlas georreferenciado: registra producciones que usaron el atlas
        # (lugar/vista/ref/cámara quedan en el catálogo, recuperables).
        self._atlas = atlas_service

    def _record_atlas_production(self, state: ProductionState) -> None:
        """Registra en el atlas la producción si usó lugar/vista (metadata['atlas']).

        Recuperable tras reiniciar: queda en catalog.db (tabla productions) con
        versión del atlas, cámara y referencias exactas. Nunca falla la producción
        si el atlas no está disponible.
        """
        atlas_ctx = (state.metadata or {}).get("atlas")
        if not atlas_ctx or self._atlas is None:
            return
        try:
            from comfy_studio.atlas.models import ProductionRecord

            if not self._atlas.available():
                return
            self._atlas.store.record_production(ProductionRecord(
                prod_id=state.id,
                atlas_version=self._atlas.atlas_version(),
                place_id=atlas_ctx.get("place_id"),
                view_id=atlas_ctx.get("view_id"),
                scenario_id=atlas_ctx.get("scenario_id"),
                camera=dict(atlas_ctx.get("camera") or {}),
                params={
                    "idea": state.idea[:500],
                    "phase": state.phase,
                    "ref_id": atlas_ctx.get("ref_id"),
                    "background_path": atlas_ctx.get("background_path"),
                    "limitations": atlas_ctx.get("limitations") or [],
                },
                result={
                    "status": state.phase,
                    "final_output_path": state.final_output_path,
                },
            ))
            state.record("atlas_production_recorded", place_id=atlas_ctx.get("place_id"))
        except Exception:  # noqa: BLE001
            pass

    def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Publica un evento de producción en el bus SSE (thread-safe si hay loop)."""
        if self.broker is not None:
            self.broker.publish_threadsafe(event_type, data)

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
        state.metadata["execution_mode"] = "real" if self._fully_wired else "simulated"
        state.metadata["simulated"] = not self._fully_wired
        _plan(state, self.collaborators.planner)
        if self.resource_vram_budget_mb is not None:
            binding = bind_recipes(
                state,
                vram_budget_mb=self.resource_vram_budget_mb,
            )
            state.metadata["recipe_binding"] = {
                **binding,
                "vram_budget_mb": self.resource_vram_budget_mb,
                "estimate_only": True,
            }
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
        self._publish(
            "production_planned",
            {
                "production_id": state.id,
                "project_id": state.metadata.get("project_id"),
                "phase": state.phase,
                "shots": len(state.all_shots()),
            },
        )
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

    def list_productions(
        self,
        *,
        project_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lista producciones (checkpoints), opcionalmente filtradas por proyecto."""
        out: list[dict[str, Any]] = []
        wanted = max(limit, 1)
        for pid in self.checkpointer.list_ids(limit=max(wanted * 4, 100)):
            state = self.checkpointer.load(pid)
            if state is None:
                continue
            self._ensure_artifact_semantics(state)
            self._record_atlas_production(state)
            if project_id and state.metadata.get("project_id") != project_id:
                continue
            out.append(self._status_dict(state))
            if len(out) >= wanted:
                break
        return out

    def update_plan(
        self,
        production_id: str,
        *,
        idea: str | None = None,
        shots: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Edita el plan SOLO en `awaiting_approval`; persiste vía checkpointer.

        `shots` es una lista de ediciones {id, description?, duration_s?, remove?}
        sobre los shots existentes. Fail-closed: cualquier id desconocido aborta
        toda la edición (no se guarda nada parcial).
        """
        state = self._load(production_id)
        if state.phase != "awaiting_approval":
            raise ProductionPlanNotEditableError(
                f"el plan solo es editable en awaiting_approval, no en {state.phase}"
            )
        if idea is not None:
            state.idea = idea
        if shots is not None:
            by_id = {s.id: s for s in state.all_shots()}
            for edit in shots:
                shot = by_id.get(str(edit.get("id") or ""))
                if shot is None:
                    raise LookupError(
                        f"shot {edit.get('id')!r} no existe en la producción"
                    )
                if edit.get("remove"):
                    for seq in state.sequences:
                        if shot in seq.shots:
                            seq.shots.remove(shot)
                            break
                    for other in state.all_shots():
                        if shot.id in other.depends_on:
                            other.depends_on.remove(shot.id)
                    continue
                if edit.get("description") is not None:
                    shot.description = str(edit["description"])
                if edit.get("duration_s") is not None:
                    shot.duration_s = float(edit["duration_s"])
            if not state.all_shots():
                raise ValueError("el plan no puede quedar sin shots")
        state.record(
            "plan_updated",
            idea=idea is not None,
            edits=len(shots or []),
        )
        self.checkpointer.save(state)
        self._publish(
            "production_plan_updated",
            {
                "production_id": state.id,
                "project_id": state.metadata.get("project_id"),
                "phase": state.phase,
                "shots": len(state.all_shots()),
            },
        )
        return {
            "production_id": production_id,
            "phase": state.phase,
            "plan": state.to_dict(),
        }

    def cancel_production(self, production_id: str) -> dict[str, Any]:
        """Cancela la producción: terminal si espera aprobación, cooperativo si ejecuta."""
        state = self._load(production_id)
        if state.phase in _TERMINAL_PHASES:
            raise ProductionAlreadyTerminalError(
                f"la producción ya es terminal ({state.phase}); no admite cancelación"
            )
        if state.phase == "awaiting_approval":
            state.phase = "cancelled"
            state.record("cancelled", stage="awaiting_approval")
            event = "production_cancelled"
        else:
            state.metadata["cancel_requested"] = True
            state.record("cancel_requested", stage=state.phase)
            self._cancel_requested.add(production_id)
            event = "production_cancel_requested"
        self.checkpointer.save(state)
        self._publish(
            event,
            {
                "production_id": state.id,
                "project_id": state.metadata.get("project_id"),
                "phase": state.phase,
            },
        )
        return self._status_dict(state)

    def approve_plan(self, production_id: str) -> dict[str, Any]:
        """El humano aprueba; corre el lazo caro y devuelve el resultado."""
        state = self._load(production_id)
        # una producción cancelada por el humano no se puede aprobar (fail-closed);
        # el resto de fases terminales (done/failed) mantienen el approve idempotente.
        if state.phase == "cancelled":
            raise ProductionNotApprovedError(
                "la producción fue cancelada; no se puede aprobar"
            )
        if state.phase in _TERMINAL_PHASES:
            if state.phase in ("done", "simulated"):
                registered = state.metadata.get("output_asset_ids") or []
                return {
                    "production_id": production_id,
                    "phase": state.phase,
                    "final_output_path": state.final_output_path,
                    "output_asset_ids": registered,
                    "status": self._status_dict(state),
                }
            raise ProductionAlreadyTerminalError(
                f"la producción ya es terminal ({state.phase})"
            )
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
        # persistir la transición ANTES de correr el lazo: si no, un cancel que
        # llegue antes del primer checkpoint del grafo leería `awaiting_approval`
        # y tomaría la vía terminal mientras el lazo arranca igualmente (M7-AGT-001).
        self.checkpointer.save(state)

        # correr el lazo caro (ejecución + evaluación + replan + ensamblaje).
        graph = build_production_graph(
            checkpointer=self.checkpointer,
            collaborators=self.collaborators,
        )
        if self.broker is not None:
            # progreso en vivo por SSE: un evento por paso persistido del grafo.
            graph.on_step = lambda st: self._publish(
                "production_step", self._status_dict(st)
            )
        # cancelación cooperativa en proceso: `cancel_production` durante la
        # ejecución sólo persiste el flag; el hook es lo que detiene el lazo.
        graph.should_cancel = lambda: production_id in self._cancel_requested
        self._publish(
            "production_approved",
            {
                "production_id": production_id,
                "project_id": state.metadata.get("project_id"),
                "phase": state.phase,
            },
        )
        try:
            final = graph.run(state)
        finally:
            self._cancel_requested.discard(production_id)
        self._finalize_artifact_status(final)
        registered = self._register_outputs_in_gallery(final)
        self.checkpointer.save(final)
        self._publish(
            "production_finished",
            {
                "production_id": final.id,
                "project_id": final.metadata.get("project_id"),
                "phase": final.phase,
                "output_asset_ids": registered,
            },
        )
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
        if state.metadata.get("simulated"):
            state.metadata["output_asset_ids"] = []
            return []
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

    @staticmethod
    def _finalize_artifact_status(state: ProductionState) -> None:
        """Only publish `done` for a fully wired run with a probeable video file."""
        if state.phase != "done":
            return
        if state.metadata.get("simulated"):
            state.final_output_path = None
            state.phase = "simulated"
            state.record(
                "simulation_complete",
                reason="se usó uno o más colaboradores stub; no hay artefacto de video afirmable",
            )
            return

        from pathlib import Path

        path = Path(state.final_output_path) if state.final_output_path else None
        error: str | None = None
        try:
            if path is None or not path.is_file() or path.stat().st_size <= 0:
                error = "el ensamblador no produjo un archivo de video no vacío"
            else:
                from comfy_studio.perception.keyframes import probe_media_info

                probe = probe_media_info(path)
                if not probe.get("probe_ok") or not probe.get("has_video"):
                    error = "ffprobe no verificó un stream de video en el artefacto final"
        except OSError:
            error = "no se pudo leer el archivo de video ensamblado"
        if error:
            state.final_output_path = None
            state.phase = "failed"
            state.metadata["artifact_error"] = error
            state.record(
                "artifact_verification_failed",
                reason=error,
            )
        else:
            state.metadata["artifact_verified"] = True
            state.record("artifact_verified")

    def _ensure_artifact_semantics(self, state: ProductionState) -> None:
        """Migrate old `done` checkpoints before exposing a path or status."""
        if state.phase != "done" or state.metadata.get("artifact_verified"):
            return
        if (
            state.metadata.get("execution_mode") == "real"
            and not state.metadata.get("simulated")
        ):
            self._finalize_artifact_status(state)
        else:
            state.phase = "simulated"
            state.final_output_path = None
            state.metadata["execution_mode"] = "simulated"
            state.metadata["simulated"] = True
            state.record(
                "legacy_simulation_migrated",
                reason="el checkpoint antiguo no acredita un render real",
            )
        self.checkpointer.save(state)

    def _load(self, production_id: str) -> ProductionState:
        state = self.checkpointer.load(production_id)
        if state is None:
            raise ProductionNotFoundError(production_id)
        self._ensure_artifact_semantics(state)
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
            "cancel_requested": bool(state.metadata.get("cancel_requested")),
            "final_output_path": state.final_output_path,
            "output_asset_ids": state.metadata.get("output_asset_ids") or [],
            "execution_mode": state.metadata.get("execution_mode", "unknown"),
            "simulated": bool(state.metadata.get("simulated")),
            "artifact_error": state.metadata.get("artifact_error"),
            "artifact_ready": (
                state.phase == "done"
                and bool(state.final_output_path)
                and bool(state.metadata.get("artifact_verified"))
            ),
            "shots_complete": state.is_complete(),
            "complete": state.phase in ("done", "simulated") and state.is_complete(),
        }
