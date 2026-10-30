"""Capa de orquestación del estudio (M6 Bloque 4).

Convierte una idea en una producción mediante un grafo tipado y reanudable
(Eje 4 LangGraph), sobre el contrato de motores de M4/M5 y el cerebro del
agente de M6 Bloque 1. Fases 12–16: estado + checkpointer + grafo (12),
planner jerárquico (13), ejecución adaptativa (14), feedback loop + replan
escalonado (15), ensamblaje (16).
"""

from __future__ import annotations

from comfy_studio.orchestration.adapters import (
    EngineAssembler,
    EngineRunnerAdapter,
    LLMPlannerAdapter,
    QaShotEvaluator,
    build_server_collaborators,
)
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
    make_template_reselector,
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
    replan_shot_focalized,
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
    "EngineAssembler",
    "EngineRunner",
    "EngineRunnerAdapter",
    "LLMPlannerAdapter",
    "Layer",
    "PlannerLLM",
    "ProductionCheckpointer",
    "ProductionGraph",
    "ProductionNotApprovedError",
    "ProductionNotFoundError",
    "ProductionService",
    "ProductionState",
    "QaShotEvaluator",
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
    "build_server_collaborators",
    "evaluate_and_replan",
    "execute_next_shot",
    "langgraph_available",
    "make_template_reselector",
    "orchestration_health_report",
    "plan_production",
    "replan_shot_focalized",
    "topological_shots",
]
