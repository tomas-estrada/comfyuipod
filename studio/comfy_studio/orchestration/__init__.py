"""Capa de orquestación del estudio (M6 Bloque 3+4).

Convierte una idea en una producción: selección de plantillas sobre la librería
amplia (Eje 3), y el lazo adaptativo con LangGraph (Eje 1/2/4, fases siguientes).
"""

from __future__ import annotations

from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.conductor import build_production_graph
from comfy_studio.orchestration.graph import (
    END,
    ProductionGraph,
    langgraph_available,
)
from comfy_studio.orchestration.state import (
    REPLAN_ESCALATION,
    Layer,
    ProductionState,
    ReplanTier,
    Sequence,
    Shot,
    ShotStatus,
)

__all__ = [
    "END",
    "REPLAN_ESCALATION",
    "Layer",
    "ProductionCheckpointer",
    "ProductionGraph",
    "ProductionState",
    "ReplanTier",
    "Sequence",
    "Shot",
    "ShotStatus",
    "build_production_graph",
    "langgraph_available",
]
