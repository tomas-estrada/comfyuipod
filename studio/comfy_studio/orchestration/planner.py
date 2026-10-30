"""Planificación jerárquica (M6 Bloque 4, fase 13) — Eje 1.

Descompone una idea en la jerarquía del Eje 1: **idea → secuencias → shots (3–7s)
→ capas por shot** (gen, depth, relight, comp). Es el nodo `plan` del grafo; su
salida es un `ProductionState` poblado que el resto del lazo ejecuta y evalúa.

Criterio (igual que el cerebro con Letta, y el grafo con LangGraph): NO acoplar el
grafo síncrono al cliente LLM async del estudio. Se define un **protocolo mínimo
`PlannerLLM`** (síncrono: idea+contexto → plan JSON) que el conductor inyecta. Por
defecto se usa un **stub determinista** que produce un plan plausible sin gastar
tokens — misma disciplina stub-first de los motores. El adaptador al cliente LLM
real (que envuelve la llamada async) es una implementación de ese protocolo, no una
dependencia dura de esta capa.

El plan reparte el cost cap total entre los shots y fija un `acceptance` por shot
(umbral VBench) que el evaluador del Eje 2 (fase 15) usará para decidir replan.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from comfy_studio.orchestration.state import (
    Layer,
    ProductionState,
    Sequence,
    Shot,
)

#: umbral VBench por defecto que un shot debe superar para aceptarse.
DEFAULT_VBENCH_MIN = float(os.environ.get("STUDIO_ORCH_VBENCH_MIN", "0.55"))

#: duración objetivo de un shot (dentro del rango 3–7 s del plan maestro).
DEFAULT_SHOT_SECONDS = 5.0


class PlannerLLM(Protocol):
    """Contrato mínimo que el planner necesita de un LLM (síncrono).

    Una implementación real envuelve el cliente async del estudio y devuelve el
    MISMO dict de plan; el grafo no ve el async. El stub lo implementa sin red.
    """

    def plan(self, idea: str, *, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Devuelve un plan como dict: {'sequences': [{'title', 'shots': [...]}]}."""
        ...


class StubPlannerLLM:
    """Planner determinista para CI/tests (STUDIO_STUB_PLANNER y por defecto).

    Produce un plan plausible y reproducible a partir de la idea: una o dos
    secuencias, cada una con 2–3 shots, cada shot con una capa de generación y,
    para ideas con señales de movimiento/3D, capas adicionales. Sin red, sin LLM.
    """

    #: palabras que sugieren capas extra (heurística transparente, no mágica).
    _DEPTH_HINTS = ("3d", "cámara", "camara", "dron", "vuelo", "órbita", "orbita")
    _RELIGHT_HINTS = ("noche", "neón", "neon", "amanecer", "atardecer", "luz")

    def plan(
        self, idea: str, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        idea_l = idea.lower()
        wants_depth = any(h in idea_l for h in self._DEPTH_HINTS)
        wants_relight = any(h in idea_l for h in self._RELIGHT_HINTS)

        def _layers() -> list[dict[str, Any]]:
            layers: list[dict[str, Any]] = [
                {"kind": "gen", "engine": "comfyui", "params": {"prompt": idea[:200]}}
            ]
            if wants_depth:
                layers.append({"kind": "depth", "engine": "comfyui", "params": {}})
            if wants_relight:
                layers.append({"kind": "relight", "engine": "comfyui", "params": {}})
            layers.append(
                {"kind": "upscale", "engine": "upscaler", "params": {"mode": "video"}}
            )
            return layers

        shots = [
            {"description": f"plano de apertura: {idea}"[:200], "layers": _layers()},
            {"description": f"plano medio: {idea}"[:200], "layers": _layers()},
            {"description": f"plano de cierre: {idea}"[:200], "layers": _layers()},
        ]
        return {"sequences": [{"title": "secuencia principal", "shots": shots}]}


def plan_production(
    state: ProductionState,
    llm: PlannerLLM | None = None,
    *,
    vbench_min: float = DEFAULT_VBENCH_MIN,
) -> ProductionState:
    """Puebla `state` con la jerarquía del plan (nodo `plan` del grafo).

    Si `state` ya tiene secuencias, no replanifica (idempotente). El cost cap total
    se reparte en partes iguales entre los shots. Cada shot recibe su `acceptance`.
    """
    if state.sequences:
        return state

    planner = llm or StubPlannerLLM()
    raw = planner.plan(state.idea)

    # construir la jerarquía tipada desde el dict del planner.
    sequences: list[Sequence] = []
    for seq_d in raw.get("sequences", []):
        seq = Sequence(title=str(seq_d.get("title", "secuencia")))
        for sh_d in seq_d.get("shots", []):
            shot = Shot(
                description=str(sh_d.get("description", state.idea))[:200],
                duration_s=float(sh_d.get("duration_s", DEFAULT_SHOT_SECONDS)),
                acceptance={"vbench_min": vbench_min},
                depends_on=list(sh_d.get("depends_on", [])),
            )
            shot.layers = [
                Layer(
                    kind=str(ly.get("kind", "gen")),
                    engine=str(ly.get("engine", "comfyui")),
                    template_id=ly.get("template_id"),
                    params=dict(ly.get("params", {})),
                )
                for ly in sh_d.get("layers", [])
            ]
            seq.shots.append(shot)
        sequences.append(seq)

    state.sequences = sequences

    # repartir el cost cap total por shot.
    all_shots = state.all_shots()
    if all_shots and state.cost_cap_usd > 0:
        per = state.cost_cap_usd / len(all_shots)
        for shot in all_shots:
            shot.cost_cap_usd = round(per, 4)

    state.phase = "executing"
    state.record(
        "plan",
        sequences=len(sequences),
        shots=len(all_shots),
        planner=type(planner).__name__,
    )
    return state


def replan_shot_focalized(
    shot: Shot,
    state: ProductionState,
    llm: PlannerLLM | None = None,
) -> bool:
    """Replanifica un único shot con el LLM sin purgar secuencias completadas (M6-SCR-003).

    Conserva dependencias y duración; reescribe las capas del shot con
    nuevos parámetros sugeridos por el planner para superar la evaluación.
    """
    planner = llm or StubPlannerLLM()
    context = {
        "idea": state.idea,
        "failed_shot_id": shot.id,
        "previous_description": shot.description,
        "last_score": shot.last_score,
    }
    raw = planner.plan(
        f"Reencuadre de calidad: {shot.description}", context=context
    )
    seqs = raw.get("sequences", [])
    if seqs and seqs[0].get("shots"):
        new_shot_data = seqs[0]["shots"][0]
        shot.description = str(
            new_shot_data.get("description", shot.description)
        )[:200]
        if new_shot_data.get("layers"):
            shot.layers = [
                Layer(
                    kind=str(ly.get("kind", "gen")),
                    engine=str(ly.get("engine", "comfyui")),
                    template_id=ly.get("template_id"),
                    params=dict(ly.get("params", {})),
                )
                for ly in new_shot_data.get("layers", [])
            ]
        return True
    return False
