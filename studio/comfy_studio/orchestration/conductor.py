"""Conductor esqueleto del grafo de producción (M6 Bloque 4, fase 12).

Arma el grafo del Eje 4 con sus fases como nodos y el router condicional que
decide el camino (planificar → ejecutar → evaluar → [replanificar] → ensamblar).
En fase 12 los nodos son **stubs deterministas**: demuestran que el lazo corre de
punta a punta y persiste en cada paso, sin tocar motores ni gastar GPU. Las fases
13–16 sustituyen cada stub por su lógica real (planner LLM, ejecución adaptativa
sobre los motores, evaluador VBench/FFmpeg + replanificación escalonada, y
ensamblaje final).

El router implementa la forma del lazo adaptativo (Eje 2): mientras queden shots
pendientes, ejecutar→evaluar; cuando todos están aceptados, ensamblar; luego END.
"""

from __future__ import annotations

from comfy_studio.orchestration.graph import END, ProductionGraph
from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
    ShotStatus,
)

# Fases del grafo (nombres de nodo).
PLAN = "plan"
EXECUTE = "execute"
EVALUATE = "evaluate"
ASSEMBLE = "assemble"


# ---------------------------------------------------------------------------
# Nodos stub (fase 12). Reemplazados por lógica real en fases 13–16.
# ---------------------------------------------------------------------------


def _node_plan(state: ProductionState) -> ProductionState:
    """Descompone la idea en secuencias→shots→capas (stub determinista).

    Fase 13 lo reemplaza por el planner LLM jerárquico (stub determinista propio).
    Aquí produce una secuencia de un shot con una capa, para que el lazo tenga
    algo que ejecutar.
    """
    if not state.sequences:
        shot = Shot(
            description=f"plano inicial de: {state.idea}"[:200],
            duration_s=5.0,
            acceptance={"vbench_min": 0.0},
            layers=[Layer(kind="gen", engine="comfyui")],
        )
        state.sequences.append(Sequence(title="secuencia 1", shots=[shot]))
    state.phase = "executing"
    state.record("plan", sequences=len(state.sequences), shots=len(state.all_shots()))
    return state


def _node_execute(state: ProductionState) -> ProductionState:
    """Ejecuta el siguiente shot pendiente (stub: lo marca running→evaluating).

    Fase 14 lo reemplaza por la ejecución adaptativa real (motores como nodos,
    paralelo/topológico). Aquí avanza un shot por paso para que el lazo progrese.
    """
    for shot in state.pending_shots():
        if shot.status in (ShotStatus.PLANNED, ShotStatus.REPLANNING):
            shot.status = ShotStatus.EVALUATING
            shot.output_path = f"/tmp/stub/{shot.id}.mp4"
            state.record("execute", shot=shot.id, tier=str(shot.replan_tier))
            return state
    return state


def _node_evaluate(state: ProductionState) -> ProductionState:
    """Puntúa el shot ejecutado contra su acceptance (stub: siempre pasa).

    Fase 15 lo reemplaza por el evaluador VBench+FFmpeg y la replanificación
    escalonada local→plantilla→global. Aquí acepta para cerrar el lazo.
    """
    for shot in state.all_shots():
        if shot.status == ShotStatus.EVALUATING:
            shot.last_score = {"vbench": 1.0}
            shot.status = ShotStatus.ACCEPTED
            state.record("evaluate", shot=shot.id, accepted=True)
            return state
    return state


def _node_assemble(state: ProductionState) -> ProductionState:
    """Ensambla los shots aceptados en el reel final (stub path).

    Fase 16 lo reemplaza por editor+compositor reales y el reel descargable.
    """
    state.final_output_path = f"/tmp/stub/{state.id}_reel.mp4"
    state.phase = "done"
    state.record("assemble", output=state.final_output_path)
    return state


# ---------------------------------------------------------------------------
# Router condicional (la forma del lazo adaptativo, Eje 2)
# ---------------------------------------------------------------------------


def _route(state: ProductionState) -> str:
    """Decide el siguiente nodo desde el estado."""
    if state.phase == "done":
        return END
    if not state.sequences:
        return PLAN
    # ¿hay un shot a la espera de evaluación? -> evaluar.
    if any(s.status == ShotStatus.EVALUATING for s in state.all_shots()):
        return EVALUATE
    # ¿quedan shots por ejecutar? -> ejecutar.
    if state.pending_shots():
        return EXECUTE
    # todo aceptado -> ensamblar.
    return ASSEMBLE


def build_production_graph(
    *, checkpointer=None, max_steps: int = 200
) -> ProductionGraph:
    """Construye el grafo de producción esqueleto (fase 12)."""
    graph = ProductionGraph(checkpointer=checkpointer, max_steps=max_steps)
    graph.add_node(PLAN, _node_plan)
    graph.add_node(EXECUTE, _node_execute)
    graph.add_node(EVALUATE, _node_evaluate)
    graph.add_node(ASSEMBLE, _node_assemble)
    graph.set_entry(PLAN)
    graph.set_router(_route)
    return graph
