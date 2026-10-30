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

from collections.abc import Callable

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.state import ProductionState

#: un nodo transforma el estado y lo devuelve.
NodeFn = Callable[[ProductionState], ProductionState]

#: nombre del nodo terminal.
END = "__end__"


def langgraph_available() -> bool:
    """True si la biblioteca LangGraph está instalada (horneada)."""
    try:
        import langgraph.graph  # noqa: F401
    except ImportError:
        return False
    return True


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

        Usa el runner stdlib (determinista, sin dep). El camino LangGraph comparte
        exactamente estos nodos y router; se activa en `compile_langgraph()` cuando
        la biblioteca está horneada, para no divergir el comportamiento.
        """
        if self._entry is None or self._router is None:
            raise RuntimeError("grafo incompleto: falta entry o router")

        current = self._entry
        steps = 0
        while current != END:
            if steps >= self.max_steps:
                state.record("graph_halted", reason="max_steps", at=current)
                state.phase = "halted"
                self._checkpoint(state)
                break
            node = self._nodes.get(current)
            if node is None:
                raise RuntimeError(f"nodo desconocido: {current}")
            state = node(state)
            self._checkpoint(state)
            current = self._router(state)
            steps += 1
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
        from langgraph.graph import StateGraph

        builder = StateGraph(dict)
        for name, fn in self._nodes.items():
            builder.add_node(name, _wrap_for_langgraph(fn))
        builder.set_entry_point(self._entry)
        router = self._router

        def _lg_router(lg_state: dict) -> str:
            nxt = router(ProductionState.from_dict(lg_state))
            return LG_END if nxt == END else nxt

        for name in self._nodes:
            builder.add_conditional_edges(name, _lg_router)
        return builder.compile()


def _wrap_for_langgraph(fn: NodeFn):  # pragma: no cover - requires langgraph
    """Adapta un NodeFn (ProductionState->ProductionState) a dict->dict."""

    def _inner(lg_state: dict) -> dict:
        state = ProductionState.from_dict(lg_state)
        return fn(state).to_dict()

    return _inner
