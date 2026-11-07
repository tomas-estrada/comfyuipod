"""Grafo de producción (M6 Bloque 4, fase 12) — base del Eje 4.

El lazo de producción es un grafo: **nodos = fases/motores**, **aristas
condicionales = decisiones** (aceptar / replanificar / ensamblar). El estado que
viaja es `ProductionState`; tras cada nodo se persiste con el checkpointer para
reanudar tras un corte.

Criterio sobre LangGraph (igual que Letta en el cerebro — evaluar peso/choque):
LangGraph es **biblioteca pura** (sin servidor, sin segundo proceso) y encaja con
el stack, así que se usa como motor del grafo CUANDO ESTÁ INSTALADO. Pero para que
la CI y los tests no dependan de un import pesado, el grafo tiene un **runner de
fallback propio** en la stdlib que ejecuta el mismo grafo tipado (nodos + router).
Mismo contrato que los motores: stub-first, tipado, sin dep obligatoria.

Las fases 13–16 rellenan los nodos reales (planner LLM, ejecución adaptativa,
evaluador+replan, ensamblaje). Esta fase entrega el esqueleto ejecutable: un grafo
que planifica (stub), ejecuta (stub), evalúa (stub) y ensambla (stub), persistiendo
en cada paso — demostrando que el lazo del Eje 4 corre de punta a punta.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypedDict

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.state import ProductionState

logger = logging.getLogger(__name__)

#: un nodo transforma el estado y lo devuelve.
NodeFn = Callable[[ProductionState], ProductionState]

#: hook opcional llamado tras cada paso (checkpoint incluido) para progreso en vivo.
StepHook = Callable[[ProductionState], None]

#: hook opcional de cancelación cooperativa: True = detener el lazo en el siguiente paso.
CancelHook = Callable[[], bool]

#: nombre del nodo terminal.
END = "__end__"


class _LangGraphState(TypedDict):
    """Typed wrapper keeps the complete serializable production state in one channel."""

    state: dict[str, Any]


def langgraph_available() -> bool:
    """True si la biblioteca LangGraph está instalada (horneada)."""
    from importlib.util import find_spec

    return find_spec("langgraph") is not None


class ProductionGraph:
    """Grafo tipado de la producción con checkpointing por paso.

    Registra nodos y un router (aristas condicionales) y los corre con LangGraph
    si está disponible, o con el runner stdlib equivalente si no. El resultado es
    el mismo `ProductionState` final; la diferencia es sólo el motor de ejecución.
    """

    def __init__(
        self,
        *,
        checkpointer: ProductionCheckpointer | None = None,
        max_steps: int = 200,
    ) -> None:
        self._nodes: dict[str, NodeFn] = {}
        self._router: Callable[[ProductionState], str] | None = None
        self._entry: str | None = None
        self.checkpointer = checkpointer
        self.max_steps = max_steps
        #: hook de progreso por paso (p.ej. publicar SSE); nunca rompe el grafo.
        self.on_step: StepHook | None = None
        #: hook de cancelación en proceso (p.ej. cancelRequested por otra petición).
        self.should_cancel: CancelHook | None = None

    # -- construcción --------------------------------------------------------

    def add_node(self, name: str, fn: NodeFn) -> None:
        if name == END:
            raise ValueError(f"'{END}' es un nombre reservado")
        self._nodes[name] = fn

    def set_entry(self, name: str) -> None:
        self._entry = name

    def set_router(self, router: Callable[[ProductionState], str]) -> None:
        """El router decide el siguiente nodo (o END) desde el estado."""
        self._router = router

    # -- ejecución -----------------------------------------------------------

    def run(self, state: ProductionState) -> ProductionState:
        """Ejecuta el grafo de punta a punta, persistiendo en cada paso.

        Usa LangGraph cuando está instalado en la imagen; cada nodo comparte el
        mismo checkpoint SQLite que el runner stdlib. El fallback conserva ese
        contrato para entornos mínimos y fixtures CPU.
        """
        if self._entry is None or self._router is None:
            raise RuntimeError("grafo incompleto: falta entry o router")

        if langgraph_available():
            compiled = self.compile_langgraph()
            if compiled is None:  # pragma: no cover - availability was just checked
                raise RuntimeError("LangGraph está disponible pero no se pudo compilar")
            final = compiled.invoke(
                {"state": state.to_dict()},
                config={"recursion_limit": max(self.max_steps + 5, 25)},
            )
            return ProductionState.from_dict(final["state"])

        # The SQLite snapshot carries both the node that was in flight and the
        # node selected after the last completed step. A fresh approval starts
        # at the route implied by its persisted plan; a restarted process resumes
        # the in-flight node or the next safe node boundary.
        inflight = state.metadata.get("_graph_inflight_node")
        pending = state.metadata.get("_graph_next_node")
        current = inflight or pending or self._router(state)
        steps = int(state.metadata.get("_graph_steps", 0))
        while current != END:
            if steps >= self.max_steps:
                state.record("graph_halted", reason="max_steps", at=current)
                state.phase = "halted"
                self._checkpoint(state)
                break
            node = self._nodes.get(current)
            if node is None:
                raise RuntimeError(f"nodo desconocido: {current}")
            state.metadata["_graph_inflight_node"] = current
            state.metadata.pop("_graph_next_node", None)
            state.record("graph_step_started", node=current)
            self._checkpoint(state)
            state = node(state)
            if self.should_cancel is not None and self.should_cancel():
                # cancelación cooperativa llegada durante el paso (M7-AGT-001):
                # el estado en vuelo no ve el flag persistido por otra petición,
                # así que el hook en proceso es la vía que sí llega al lazo.
                state.phase = "cancelled"
                state.metadata["cancel_requested"] = True
                state.metadata.pop("_graph_inflight_node", None)
                state.metadata.pop("_graph_next_node", None)
                state.record("cancelled", stage="execution")
                self._checkpoint(state)
                if self.on_step is not None:
                    try:
                        self.on_step(state)
                    except Exception:
                        logger.exception("on_step del grafo falló en el paso %s", current)
                return state
            completed_node = current
            current = self._router(state)
            steps += 1
            state.metadata["_graph_steps"] = steps
            if steps >= self.max_steps and current != END:
                state.phase = "halted"
                state.record("graph_halted", reason="max_steps", at=current)
                current = END
            state.metadata.pop("_graph_inflight_node", None)
            if current == END:
                state.metadata.pop("_graph_next_node", None)
            else:
                state.metadata["_graph_next_node"] = current
            state.record("graph_step_completed", node=completed_node, next=current)
            self._checkpoint(state)
            if self.on_step is not None:
                try:
                    self.on_step(state)
                except Exception:
                    logger.exception("on_step del grafo falló en el paso %s", current)
        return state

    def _checkpoint(self, state: ProductionState) -> None:
        if self.checkpointer is not None:
            self.checkpointer.save(state)

    def compile_langgraph(self):  # pragma: no cover - requires langgraph baked
        """Compila el MISMO grafo con LangGraph si está instalado.

        Devuelve el grafo compilado de LangGraph, o None si la biblioteca no está.
        Reutiliza los nodos y el router ya registrados — no duplica lógica.
        """
        if not langgraph_available():
            return None
        from langgraph.graph import END as LG_END
        from langgraph.graph import START, StateGraph

        builder = StateGraph(_LangGraphState)
        for name, fn in self._nodes.items():
            builder.add_node(name, _wrap_for_langgraph(self, name, fn))

        def _lg_router(lg_state: dict) -> str:
            state = ProductionState.from_dict(lg_state["state"])
            if state.phase in {"done", "failed", "cancelled", "halted"}:
                return LG_END
            nxt = state.metadata.get("_graph_next_node") or self._router(state)
            return LG_END if nxt == END else nxt

        def _lg_prepare(lg_state: dict) -> dict:
            state = ProductionState.from_dict(lg_state["state"])
            steps = int(state.metadata.get("_graph_steps", 0))
            if steps >= self.max_steps:
                state.phase = "halted"
                state.metadata.pop("_graph_inflight_node", None)
                state.metadata.pop("_graph_next_node", None)
                state.record("graph_halted", reason="max_steps")
                self._checkpoint(state)
            return {"state": state.to_dict()}

        def _lg_entry(lg_state: dict) -> str:
            state = ProductionState.from_dict(lg_state["state"])
            if state.phase in {"done", "failed", "cancelled", "halted"}:
                return LG_END
            return (
                state.metadata.get("_graph_inflight_node")
                or state.metadata.get("_graph_next_node")
                or self._router(state)
            )

        dispatch = "__resume_dispatch__"
        if dispatch in self._nodes:
            raise ValueError(f"'{dispatch}' es un nombre de nodo reservado")
        builder.add_node(dispatch, _lg_prepare)
        builder.add_edge(START, dispatch)
        builder.add_conditional_edges(dispatch, _lg_entry)
        for name in self._nodes:
            builder.add_conditional_edges(name, _lg_router)
        return builder.compile()


def _wrap_for_langgraph(graph: ProductionGraph, name: str, fn: NodeFn):
    """Adapta un NodeFn (ProductionState->ProductionState) a dict->dict."""

    def _inner(lg_state: dict) -> dict:
        state = ProductionState.from_dict(lg_state["state"])
        state.metadata["_graph_inflight_node"] = name
        state.metadata.pop("_graph_next_node", None)
        state.record("graph_step_started", node=name)
        graph._checkpoint(state)
        state = fn(state)
        if graph.should_cancel is not None and graph.should_cancel():
            state.phase = "cancelled"
            state.metadata["cancel_requested"] = True
            state.metadata.pop("_graph_inflight_node", None)
            state.metadata.pop("_graph_next_node", None)
            state.record("cancelled", stage="execution")
        else:
            next_node = graph._router(state)
            steps = int(state.metadata.get("_graph_steps", 0)) + 1
            state.metadata["_graph_steps"] = steps
            if steps >= graph.max_steps and next_node != END:
                state.phase = "halted"
                state.record("graph_halted", reason="max_steps", at=next_node)
                next_node = END
            state.metadata.pop("_graph_inflight_node", None)
            if next_node == END:
                state.metadata.pop("_graph_next_node", None)
            else:
                state.metadata["_graph_next_node"] = next_node
            state.record("graph_step_completed", node=name, next=next_node)
        graph._checkpoint(state)
        if graph.on_step is not None:
            try:
                graph.on_step(state)
            except Exception:
                logger.exception("on_step del grafo falló en el paso %s", name)
        return {"state": state.to_dict()}

    return _inner
