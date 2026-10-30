"""Capa de orquestación del estudio (M6 Bloque 4).

Convierte una idea en una producción mediante un grafo tipado y reanudable
(Eje 4 LangGraph), sobre el contrato de motores de M4/M5 y el cerebro del
agente de M6 Bloque 1. Fases 12–16: estado + checkpointer + grafo (12),
planner jerárquico (13), ejecución adaptativa (14), feedback loop + replan
escalonado (15), ensamblaje (16).
"""

from __future__ import annotations

from comfy_studio.orchestration.assembler import (
    Assembler,
    StubAssembler,
    assemble_reel,
)
from comfy_studio.orchestration.checkpointer import ProductionCheckpointer
from comfy_studio.orchestration.conductor import (
    Collaborators,
    build_production_graph,
)
from comfy_studio.orchestration.evaluator import (
    ShotEvaluator,
    StubShotEvaluator,
    evaluate_and_replan,
)
from comfy_studio.orchestration.executor import (
    CyclicDependencyError,
    EngineRunner,
    StubEngineRunner,
    execute_next_shot,
    topological_shots,
)
from comfy_studio.orchestration.graph import (
    END,
    ProductionGraph,
    langgraph_available,
)
from comfy_studio.orchestration.health import orchestration_health_report
from comfy_studio.orchestration.planner import (
    PlannerLLM,
    StubPlannerLLM,
    plan_production,
)
from comfy_studio.orchestration.service import (
    ProductionNotApprovedError,
    ProductionNotFoundError,
    ProductionService,
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
from comfy_studio.orchestration.template_selector import (
    ChainStep,
    DiscoveredTemplate,
    DiscoveryProvider,
    ShotIntent,
    TemplateCandidate,
    TemplateChain,
    TemplateSelector,
)

__all__ = [
    "END",
    "REPLAN_ESCALATION",
    "Assembler",
    "ChainStep",
    "Collaborators",
    "CyclicDependencyError",
    "DiscoveredTemplate",
    "DiscoveryProvider",
    "EngineRunner",
    "Layer",
    "PlannerLLM",
    "ProductionCheckpointer",
    "ProductionGraph",
    "ProductionNotApprovedError",
    "ProductionNotFoundError",
    "ProductionService",
    "ProductionState",
    "ReplanTier",
    "Sequence",
    "Shot",
    "ShotEvaluator",
    "ShotIntent",
    "ShotStatus",
    "StubAssembler",
    "StubEngineRunner",
    "StubPlannerLLM",
    "StubShotEvaluator",
    "TemplateCandidate",
    "TemplateChain",
    "TemplateSelector",
    "assemble_reel",
    "build_production_graph",
    "evaluate_and_replan",
    "execute_next_shot",
    "langgraph_available",
    "orchestration_health_report",
    "plan_production",
    "topological_shots",
]
