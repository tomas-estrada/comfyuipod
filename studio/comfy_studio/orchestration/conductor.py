"""Conductor del grafo de producción (M6 Bloque 4, fases 13–16).

Arma el grafo del Eje 4 enlazando las cuatro fases reales como nodos:
  - `plan`      → planner jerárquico (fase 13, `planner.plan_production`),
  - `execute`   → ejecución adaptativa (fase 14, `executor.execute_next_shot`),
  - `evaluate`  → feedback loop + replan escalonado (fase 15,
                  `evaluator.evaluate_and_replan`),
  - `assemble`  → ensamblaje del reel (fase 16, `assembler.assemble_reel`).

Cada fase acepta un colaborador inyectable (PlannerLLM, EngineRunner,
ShotEvaluator+TemplateReselector, Assembler). Por defecto se usan los stubs
deterministas, así que el conductor corre de punta a punta en CI sin GPU ni red;
el server inyecta las implementaciones reales en producción.

El router implementa la forma del lazo adaptativo (Eje 2): mientras queden shots
pendientes, ejecutar→evaluar (con replan en medio); cuando todos están aceptados
(o fallados), ensamblar; luego END.
"""

from __future__ import annotations

from dataclasses import dataclass

from comfy_studio.orchestration.assembler import Assembler, assemble_reel
from comfy_studio.orchestration.evaluator import (
    ShotEvaluator,
    TemplateReselector,
    evaluate_and_replan,
)
from comfy_studio.orchestration.executor import EngineRunner, execute_next_shot
from comfy_studio.orchestration.graph import END, ProductionGraph
from comfy_studio.orchestration.planner import PlannerLLM, plan_production
from comfy_studio.orchestration.state import ProductionState, ShotStatus

# Fases del grafo (nombres de nodo).
PLAN = "plan"
EXECUTE = "execute"
EVALUATE = "evaluate"
ASSEMBLE = "assemble"


@dataclass(slots=True)
class Collaborators:
    """Implementaciones inyectables de cada fase (None = stub determinista)."""

    planner: PlannerLLM | None = None
    runner: EngineRunner | None = None
    evaluator: ShotEvaluator | None = None
    reselect_template: TemplateReselector | None = None
    assembler: Assembler | None = None


# ---------------------------------------------------------------------------
# Router condicional (la forma del lazo adaptativo, Eje 2)
# ---------------------------------------------------------------------------


def _route(state: ProductionState) -> str:
    """Decide el siguiente nodo desde el estado."""
    if state.phase == "done":
        return END
    if not state.sequences:
        return PLAN
    # un shot a la espera de evaluación -> evaluar.
    if any(s.status == ShotStatus.EVALUATING for s in state.all_shots()):
        return EVALUATE
    # quedan shots por ejecutar (pendientes, no definitivamente fallados) -> ejecutar.
    if state.pending_shots():
        return EXECUTE
    # nada pendiente -> ensamblar y terminar.
    return ASSEMBLE


def build_production_graph(
    *,
    checkpointer=None,
    collaborators: Collaborators | None = None,
    max_steps: int = 200,
) -> ProductionGraph:
    """Construye el grafo de producción con las 4 fases reales enlazadas."""
    col = collaborators or Collaborators()
    graph = ProductionGraph(checkpointer=checkpointer, max_steps=max_steps)

    graph.add_node(PLAN, lambda s: plan_production(s, col.planner))
    graph.add_node(EXECUTE, lambda s: execute_next_shot(s, col.runner))
    graph.add_node(
        EVALUATE,
        lambda s: evaluate_and_replan(
            s, col.evaluator, reselect_template=col.reselect_template
        ),
    )
    graph.add_node(ASSEMBLE, lambda s: assemble_reel(s, col.assembler))
    graph.set_entry(PLAN)
    graph.set_router(_route)
    return graph
