"""Ejecución adaptativa (M6 Bloque 4, fase 14) — Eje 2.

Ejecuta los shots del plan enlazando los **motores como nodos**. Respeta el orden
del Eje 2: shots independientes pueden ir en cualquier orden; shots dependientes
van en **orden topológico** (`depends_on`), p.ej. capture3d → blender →
heavy_video → compositor. Cada shot ejecuta sus capas en secuencia; el resultado
deja al shot en `EVALUATING` para que el feedback loop (fase 15) lo puntúe.

Criterio (igual que planner/grafo): NO acoplar el grafo síncrono a la
construcción de los adaptadores de motor (que viven en el server async con su
cliente httpx). Se define un **protocolo `EngineRunner`** inyectable; por defecto
un **stub determinista** que no toca GPU ni red. El runner real —que despacha al
adaptador de cada motor— se inyecta desde el server, misma forma de resultado.
"""

from __future__ import annotations

from typing import Any, Protocol

from comfy_studio.orchestration.state import (
    ProductionState,
    Shot,
    ShotStatus,
)


class EngineRunner(Protocol):
    """Contrato mínimo para ejecutar una capa en un motor (síncrono).

    La implementación real envuelve `Engine.run(EngineRunSpec)` del adaptador
    correspondiente y devuelve un dict con al menos {'ok': bool, 'output_path'?,
    'error'?}. El stub lo cumple sin backend.
    """

    def run_layer(
        self, engine: str, job: dict[str, Any], *, cost_cap: float | None = None
    ) -> dict[str, Any]:
        """Ejecuta una capa en `engine`; devuelve resultado tipado como dict."""
        ...


class StubEngineRunner:
    """Runner determinista para CI/tests: 'ejecuta' escribiendo una ruta stub."""

    def run_layer(
        self, engine: str, job: dict[str, Any], *, cost_cap: float | None = None
    ) -> dict[str, Any]:
        kind = job.get("kind", "gen")
        return {
            "ok": True,
            "engine": engine,
            "output_path": f"/tmp/stub/{engine}_{kind}.out",
        }


class CyclicDependencyError(RuntimeError):
    """Los `depends_on` de los shots forman un ciclo (no ordenable)."""


def topological_shots(state: ProductionState) -> list[Shot]:
    """Shots en orden topológico por `depends_on`; estable para los libres.

    Los shots sin dependencias conservan su orden de plan. Lanza
    `CyclicDependencyError` si hay un ciclo (fail-closed, no se adivina un orden).
    """
    shots = state.all_shots()
    by_id = {s.id: s for s in shots}
    # solo contamos dependencias que existen en el plan (ignora ids huérfanos).
    indeg = {s.id: 0 for s in shots}
    deps: dict[str, list[str]] = {s.id: [] for s in shots}
    for s in shots:
        for dep in s.depends_on:
            if dep in by_id:
                deps[s.id].append(dep)
                indeg[s.id] += 1

    ordered: list[Shot] = []
    # cola estable: shots en orden de plan cuyo indegree llega a 0.
    ready = [s.id for s in shots if indeg[s.id] == 0]
    seen: set[str] = set()
    while ready:
        sid = ready.pop(0)
        if sid in seen:
            continue
        seen.add(sid)
        ordered.append(by_id[sid])
        # al "completar" sid, baja el indegree de quien dependía de él.
        for other in shots:
            if sid in deps[other.id] and other.id not in seen:
                indeg[other.id] -= 1
                if indeg[other.id] == 0:
                    ready.append(other.id)
    if len(ordered) != len(shots):
        raise CyclicDependencyError(
            "dependencias de shots con ciclo; no se puede ordenar"
        )
    return ordered


def execute_next_shot(
    state: ProductionState, runner: EngineRunner | None = None
) -> ProductionState:
    """Ejecuta el siguiente shot ejecutable (nodo `execute` del grafo).

    'Ejecutable' = pendiente, en estado PLANNED/REPLANNING, y con todas sus
    dependencias ya aceptadas. Corre sus capas en orden y lo deja EVALUATING.
    """
    run = runner or StubEngineRunner()
    accepted = {
        s.id for s in state.all_shots() if s.status == ShotStatus.ACCEPTED
    }
    failed = {s.id for s in state.all_shots() if s.status == ShotStatus.FAILED}
    known = {s.id for s in state.all_shots()}

    for shot in topological_shots(state):
        if shot.status not in (ShotStatus.PLANNED, ShotStatus.REPLANNING):
            continue
        # una dependencia INEXISTENTE (p.ej. un id alucinado por el LLM) nunca
        # estará en accepted ni en failed -> el shot se saltaría para siempre y
        # el router giraría hasta max_steps. Es inalcanzable: fallar en claro.
        missing_deps = [dep for dep in shot.depends_on if dep not in known]
        if missing_deps:
            shot.status = ShotStatus.FAILED
            shot.error = f"dependencia inexistente: {', '.join(missing_deps)}"[:500]
            state.record(
                "shot_unreachable", shot=shot.id, missing_deps=missing_deps
            )
            return state
        # una dependencia que falló definitivamente hace el shot INALCANZABLE:
        # propagar el fallo en vez de dejarlo pendiente para siempre (que haría
        # girar el grafo hasta max_steps sin avanzar).
        blocked_by_failed = [dep for dep in shot.depends_on if dep in failed]
        if blocked_by_failed:
            shot.status = ShotStatus.FAILED
            shot.error = f"dependencia fallida: {', '.join(blocked_by_failed)}"[:500]
            state.record(
                "shot_unreachable", shot=shot.id, failed_deps=blocked_by_failed
            )
            return state
        # no ejecutar hasta que sus dependencias estén aceptadas.
        if any(dep not in accepted for dep in shot.depends_on):
            continue

        shot.status = ShotStatus.RUNNING
        last_output: str | None = None
        for layer in shot.layers:
            job = {"kind": layer.kind, "shot": shot.id, **layer.params}
            if layer.template_id:
                job["template_id"] = layer.template_id
            result = run.run_layer(
                layer.engine, job, cost_cap=shot.cost_cap_usd or None
            )
            if not result.get("ok", False):
                shot.error = str(result.get("error", "fallo de capa sin detalle"))[:500]
                # un fallo de ejecución escala el replan igual que un fallo de
                # aceptación: local->plantilla->global->FAILED. Así un runner que
                # siempre falla no deja el shot girando en REPLANNING para siempre.
                tier = shot.escalate_replan()
                state.record(
                    "execute_failed",
                    shot=shot.id,
                    layer=layer.kind,
                    engine=layer.engine,
                    tier=str(tier) if tier else None,
                )
                return state
            layer.output_path = result.get("output_path")
            last_output = layer.output_path

        shot.output_path = last_output
        shot.error = None
        shot.status = ShotStatus.EVALUATING
        state.record(
            "execute",
            shot=shot.id,
            layers=len(shot.layers),
            tier=str(shot.replan_tier) if shot.replan_tier else None,
        )
        return state

    return state
