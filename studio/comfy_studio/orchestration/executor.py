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

import hashlib
import math
from collections.abc import Callable
from threading import Event
from typing import Any, Protocol

from comfy_studio.orchestration.lineage import LineageGraph
from comfy_studio.orchestration.state import (
    ProductionState,
    Shot,
    ShotStatus,
)

#: Claves del job que son de la orquestación, no del motor. `layer.params` viene del
#: planner (un LLM), así que no puede fijarlas: falsearían la identidad del job
#: (dedupe por idempotency_key, ownership del checkpoint y proyecto activo).
_RESERVED_JOB_KEYS = frozenset(
    {
        "kind",
        "shot",
        "job_id",
        "idempotency_key",
        "project_id",
        "template_id",
    }
)


class EngineRunner(Protocol):
    """Contrato mínimo para ejecutar una capa en un motor (síncrono).

    La implementación real envuelve `Engine.run(EngineRunSpec)` del adaptador
    correspondiente y devuelve un dict con al menos {'ok': bool, 'output_path'?,
    'error'?}. El stub lo cumple sin backend.
    """

    def run_layer(
        self,
        engine: str,
        job: dict[str, Any],
        *,
        cost_cap: float | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        """Ejecuta una capa en `engine`; devuelve resultado tipado como dict.

        El resultado PUEDE incluir `'cost_usd'`: el gasto real del run, que el
        executor acumula en `shot.cost_spent_usd` y marca `spend_reported` (P1).
        Los adaptadores de motor actuales NO lo producen — mientras ningún
        runner lo reporte, el accounting de gasto se expone como desconocido.
        Ausente o no numérico se trata como 0; nunca rompe el run.
        """
        ...


class StubEngineRunner:
    """Runner determinista para CI/tests: 'ejecuta' escribiendo una ruta stub."""

    def run_layer(
        self,
        engine: str,
        job: dict[str, Any],
        *,
        cost_cap: float | None = None,
        cancel_event: Event | None = None,
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


# --- lineage de producción (W04 / P1) ---------------------------------------

#: granularidad del DAG de lineage en producción: un nodo por SHOT. Sus entradas
#: capturan todo lo que determina su resultado (descripción, duración y el stack
#: completo de capas), de modo que un replan que cambie plantilla/params/descripción
#: produce un fingerprint nuevo y la invalidación se propaga SOLO a los
#: descendientes (`depends_on`), nunca lateralmente.


def _shot_lineage_inputs(shot: Shot) -> dict[str, Any]:
    """Entradas con hash de contenido que determinan el resultado de un shot."""
    return {
        "description": shot.description,
        "duration_s": shot.duration_s,
        "layers": [
            {
                "kind": ly.kind,
                "engine": ly.engine,
                "template_id": ly.template_id,
                "params": dict(ly.params),
            }
            for ly in shot.layers
        ],
    }


def _load_lineage(state: ProductionState) -> LineageGraph:
    """Reconstruye el DAG de lineage desde el estado serializado."""
    return LineageGraph.from_dict(state.lineage or {})


def record_shot_lineage(state: ProductionState, shot: Shot) -> None:
    """Registra la procedencia de un shot recién computado (nodo `execute`).

    Crea/actualiza el nodo del shot con sus entradas actuales, refresca los
    fingerprints y fija el artifact producido. Persiste en `state.lineage`
    (dict JSON) para que el checkpointer lo guarde con el checkpoint.
    """
    graph = _load_lineage(state)
    if graph.get(shot.id) is None:
        graph.add(
            shot.id,
            "shot",
            inputs=_shot_lineage_inputs(shot),
            depends_on=list(shot.depends_on),
        )
    else:
        # tras un replan el nodo ya existe: sus entradas nuevas ya las anotó
        # `invalidate_shot_lineage`; reflejarlas es idempotente y mantiene el
        # nodo verídico aunque el replan viniera por otra vía.
        graph.update_inputs(shot.id, **_shot_lineage_inputs(shot))
    graph.compute_fingerprints()
    if shot.output_path:
        graph.set_computed(shot.id, shot.output_path)
    state.lineage = graph.to_dict()


def invalidate_shot_lineage(state: ProductionState, shot: Shot) -> None:
    """Invalida la procedencia de un shot que entra en replan (nodo `evaluate`).

    El reintento descarta el artifact propio del shot (así lo hace
    `Shot.escalate_replan` con los `output_path`), y si sus entradas cambiaron
    (escalón plantilla/global) el fingerprint cambia y la invalidación se
    propaga por el DAG SOLO a sus descendientes: los shots de otras ramas
    conservan artifact y fingerprint (A05).

    La invalidación NO es solo metadata del DAG: los descendientes stale se
    reconcilian con el estado de producción real (`_invalidate_stale_shots`)
    para que sus outputs previos no se ensamblen ni se registren, y queden
    elegibles para recompute en el executor.
    """
    graph = _load_lineage(state)
    if graph.get(shot.id) is None:
        # sin procedencia registrada (p.ej. checkpoint anterior al wiring):
        # nada que invalidar.
        return
    previous = {
        n.node_id: n.fingerprint for n in graph.nodes() if n.fingerprint is not None
    }
    graph.update_inputs(shot.id, **_shot_lineage_inputs(shot))
    stale = graph.plan_recompute(previous)
    # aunque las entradas no cambien (escalón local), el artifact propio se
    # descarta: el reintento lo regenera.
    stale.add(shot.id)
    graph.invalidate_artifacts(stale)
    state.lineage = graph.to_dict()
    _invalidate_stale_shots(state, origin_id=shot.id, stale_ids=stale)


def _invalidate_stale_shots(
    state: ProductionState, *, origin_id: str, stale_ids: set[str]
) -> None:
    """Alinea el ESTADO de producción con la invalidación de lineage (P1).

    Un descendiente cuyo fingerprint quedó stale no puede seguir exhibiendo el
    output calculado del artifact viejo: se ensamblaría en el reel y se
    registraría en la galería como si fuera vigente. Se le descartan los
    outputs, se marca REPLANNING (elegible para recompute: el executor lo
    re-ejecuta cuando su dependencia vuelva a ACCEPTED) y se incrementa
    `replan_attempts` — el `idempotency_key` lo incluye, así un leased runner
    no deduplica el recompute contra el job VIEJO. Los shots de otras ramas
    (no stale) no se tocan; un descendiente FAILED no se resucita.
    """
    for node_id in sorted(stale_ids):
        if node_id == origin_id:
            # el propio shot ya lo gestionó `escalate_replan` (outputs fuera,
            # REPLANNING, attempts+1).
            continue
        target = state.shot_by_id(node_id)
        if target is None or target.status == ShotStatus.FAILED:
            continue
        already_recomputing = target.status in (
            ShotStatus.PLANNED,
            ShotStatus.REPLANNING,
        )
        had_outputs = (
            target.output_path is not None
            or any(ly.output_path for ly in target.layers)
        )
        if already_recomputing and not had_outputs:
            # sin outputs vivos y ya pendiente de ejecución: su próximo
            # intento ya tiene identidad de op fresca (attempts de su propia
            # escalera); nada que descartar.
            continue
        for ly in target.layers:
            ly.output_path = None
        target.output_path = None
        target.replan_attempts += 1
        target.status = ShotStatus.REPLANNING
        state.record(
            "recompute_queued",
            shot=target.id,
            origin=origin_id,
            reason="lineage_stale",
        )


def execute_next_shot(
    state: ProductionState,
    runner: EngineRunner | None = None,
    *,
    cancel_event: Event | None = None,
    checkpoint: Callable[[ProductionState], None] | None = None,
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
        if shot.status not in (
            ShotStatus.PLANNED, ShotStatus.REPLANNING, ShotStatus.RUNNING
        ):
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
        if checkpoint is not None:
            checkpoint(state)
        last_output: str | None = None
        for layer_index, layer in enumerate(shot.layers):
            if cancel_event is not None and cancel_event.is_set():
                return state
            if layer.output_path:
                last_output = layer.output_path
                continue
            key_material = (
                f"{state.id}\0{shot.id}\0{layer_index}\0{shot.replan_attempts}"
            )
            run_key = "prod_" + hashlib.sha256(key_material.encode()).hexdigest()[:40]
            state.metadata["_active_layer"] = {
                "shot_id": shot.id,
                "layer_index": layer_index,
                "idempotency_key": run_key,
            }
            if checkpoint is not None:
                checkpoint(state)
            job = {"kind": layer.kind, "shot": shot.id}
            # Las claves que la orquestación posee no pueden venir de `layer.params`.
            # El planner es un LLM: sin este filtro un params con `kind`, `shot`,
            # `job_id`, `idempotency_key`, `project_id` o `template_id` falsificados
            # se colaba por el splat y falseaba la identidad del job (dedupe,
            # ownership del checkpoint y proyecto activo).
            job.update(
                {k: v for k, v in layer.params.items() if k not in _RESERVED_JOB_KEYS}
            )
            # Engine adapters receive a stable operation id. Retries after a
            # process restart must reconcile or deduplicate this same layer,
            # rather than creating an unrelated paid/render job.
            job["job_id"] = run_key
            job["idempotency_key"] = run_key
            project_id = state.metadata.get("project_id")
            if project_id:
                job["project_id"] = project_id
            if layer.template_id:
                job["template_id"] = layer.template_id
            # P1: el gasto es CUMULATIVO entre intentos. El cap que se arma en
            # cada intento es el RESTANTE (cap - gastado), no el original: un
            # shot que ya gastó en escalones previos no puede volver a disponer
            # del cap completo. Sin cap (0) sigue siendo None = sin tope.
            if shot.cost_cap_usd > 0:
                # fail-closed ANTES de invocar cualquier runner: agotado el cap
                # no se llama al runner ni con cost_cap=0, porque 0 puede ser
                # interpretado aguas abajo como "sin tope" (P1). Solo se puede
                # gated por presupuesto cuando el runner REPORTA gasto.
                if shot.cost_spent_usd >= shot.cost_cap_usd:
                    tier = shot.escalate_replan()
                    invalidate_shot_lineage(state, shot)
                    state.record(
                        "budget_exhausted",
                        shot=shot.id,
                        layer=layer.kind,
                        engine=layer.engine,
                        tier=str(tier) if tier else None,
                        cost_cap_usd=shot.cost_cap_usd,
                        cost_spent_usd=shot.cost_spent_usd,
                    )
                    return state
                cost_cap = round(shot.cost_cap_usd - shot.cost_spent_usd, 6)
            else:
                cost_cap = None
            result = run.run_layer(
                layer.engine,
                job,
                cost_cap=cost_cap,
                cancel_event=cancel_event,
            )
            # el accounting es honesto: un run fallido también puede haber
            # consumido gasto; se acumula si el runner lo reporta.
            try:
                spent = float(result["cost_usd"])
            except (KeyError, TypeError, ValueError):
                spent = None
            if spent is not None and math.isfinite(spent) and spent >= 0:
                shot.cost_spent_usd = round(shot.cost_spent_usd + spent, 6)
                shot.spend_reported = True
                state.record(
                    "layer_spend",
                    shot=shot.id,
                    layer=layer.kind,
                    engine=layer.engine,
                    tier=str(shot.replan_tier) if shot.replan_tier else None,
                    spent_usd=spent,
                    cumulative_usd=shot.cost_spent_usd,
                )
            if not result.get("ok", False):
                shot.error = str(result.get("error", "fallo de capa sin detalle"))[:500]
                # un fallo de ejecución escala el replan igual que un fallo de
                # aceptación: local->plantilla->global->FAILED. Así un runner que
                # siempre falla no deja el shot girando en REPLANNING para siempre.
                tier = shot.escalate_replan()
                # los artifacts del intento se descartaron: reflejarlo en lineage.
                invalidate_shot_lineage(state, shot)
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
            state.metadata.pop("_active_layer", None)
            if checkpoint is not None:
                checkpoint(state)

        shot.output_path = last_output
        shot.error = None
        # P1 (W04): procedencia del shot computado — nodo con fingerprint y
        # artifact en el DAG de lineage, persistido con el checkpoint.
        record_shot_lineage(state, shot)
        shot.status = ShotStatus.EVALUATING
        state.record(
            "execute",
            shot=shot.id,
            layers=len(shot.layers),
            tier=str(shot.replan_tier) if shot.replan_tier else None,
        )
        return state

    return state
