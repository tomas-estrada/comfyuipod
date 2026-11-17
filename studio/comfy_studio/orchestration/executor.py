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
from pathlib import Path
from threading import Event
from typing import Any, Protocol

from comfy_studio.orchestration.failures import (
    MAX_TRANSIENT_RETRIES,
    LayerFailureKind,
    classify_runner_failure,
)
from comfy_studio.orchestration.lineage import LineageGraph
from comfy_studio.orchestration.state import (
    ProductionState,
    ProductionTerminalReason,
    ReplanTier,
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
        # Workflow graphs are executable engine input. They must come from the
        # trusted template/recipe resolver, never from planner-generated layer
        # params (which may contain untrusted model output).
        "graph",
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
        executor acumula en `shot.cost_spent_usd` y en el gasto de la MISIÓN
        (`state.cost_spent_usd`), marcando `spend_reported` en ambos (P1).
        Los adaptadores de motor actuales NO lo producen — mientras ningún
        runner lo reporte, el accounting de gasto se expone como desconocido.
        Ausente o no numérico se trata como 0; nunca rompe el run.

        En un fallo (`'ok': False`) el resultado PUEDE declarar
        `'failure_kind'` (`'transient'` | `'invalid_parameters'` |
        `'permission_denied'` | `'engine'`): ver
        `comfy_studio.orchestration.failures`. Sin declaración el executor
        aplica el comportamiento legacy (escalera de replan, sin reintentos).
        """
        ...


class StubEngineRunner:
    """Runner determinista para CI/tests: 'ejecuta' escribiendo un artefacto stub.

    Escribe bytes reales en la ruta que declara: el executor valida que todo
    `output_path` reutilizado exista y tenga contenido, así que un stub que
    declara una ruta sin fichero sería un artefacto fantasma en cada reinicio.
    """

    def run_layer(
        self,
        engine: str,
        job: dict[str, Any],
        *,
        cost_cap: float | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        kind = job.get("kind", "gen")
        output = Path("/tmp/stub") / f"{engine}_{kind}.out"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"stub artifact\n")
        return {
            "ok": True,
            "engine": engine,
            "output_path": str(output),
        }


class CyclicDependencyError(RuntimeError):
    """Los `depends_on` de los shots forman un ciclo (no ordenable)."""


def _mission_budget_exhausted(state: ProductionState) -> bool:
    """True si el presupuesto REPORTADO de la misión está agotado (P1).

    Solo se puede gated por presupuesto cuando algún runner REPORTA gasto
    (`spend_reported`): sin medición, `cost_spent_usd` es un límite inferior
    vacío, no un hecho, y excluir intentos por 0.0 sería fail-closed injusto.
    Un cap de misión 0 significa «sin tope definido» (comportamiento legacy),
    nunca agotado.
    """
    return (
        state.cost_cap_usd > 0
        and state.spend_reported
        and state.cost_spent_usd >= state.cost_cap_usd
    )


def _remaining_attempt_cap(state: ProductionState, shot: Shot) -> float | None:
    """Runner allowance bounded by both the shot and mission budgets."""
    caps: list[float] = []
    if shot.cost_cap_usd > 0:
        caps.append(shot.cost_cap_usd - shot.cost_spent_usd)
    if state.cost_cap_usd > 0:
        caps.append(state.cost_cap_usd - state.cost_spent_usd)
    if not caps:
        return None
    return round(max(0.0, min(caps)), 6)


def _mark_mission_budget_exhausted(
    state: ProductionState, shot: Shot, *, trigger: str
) -> None:
    """Termina la PRODUCCIÓN por presupuesto de misión agotado (P1).

    Con el restante de misión en 0 no hay receta siguiente factible que
    comprar: ningún intento adicional (de este shot ni de ningún otro) puede
    producir nada. Termina tipado con `budget_exhausted` vía
    `mark_terminal_failure` en vez de girar hasta un `max_steps` inespecífico;
    la razón viaja en el checkpoint.
    """
    fields = dict(
        shot=shot.id,
        trigger=trigger,
        cost_cap_usd=state.cost_cap_usd,
        cost_spent_usd=state.cost_spent_usd,
    )
    state.record("mission_budget_exhausted", **fields)
    state.mark_terminal_failure(
        str(ProductionTerminalReason.BUDGET_EXHAUSTED), **fields
    )


def _record_budget_exhausted(
    state: ProductionState, shot: Shot, layer: Any, tier: ReplanTier | None
) -> None:
    """Marca el shot cuando su escalera se agota POR PRESUPUESTO (P1).

    `tier is None` = local→template→global agotada y presupuesto remanente del
    shot en 0: no queda receta siguiente factible. El shot queda FAILED con
    `budget_exhausted` PERSISTIDO en el checkpoint — el ensamblador distingue
    este fallo de uno de calidad y no maquilla la producción como `done` sin
    clips (termina `failed`/`budget_exhausted`).
    """
    if tier is None:
        shot.budget_exhausted = True
    state.record(
        "budget_exhausted",
        shot=shot.id,
        layer=layer.kind,
        engine=layer.engine,
        tier=str(tier) if tier else None,
        ladder_exhausted=tier is None,
        cost_cap_usd=shot.cost_cap_usd,
        cost_spent_usd=shot.cost_spent_usd,
    )


def _mission_spend_seen(state: ProductionState) -> list[str]:
    """Claves de intentos ya contabilizados en el gasto de la misión.

    Vive en `state.metadata` para VIAJAR en el checkpoint: un replay tras
    corte que re-obtenga el resultado deduplicado del runner no vuelve a
    sumar el mismo intento (no-double-count).
    """
    seen = state.metadata.get("_mission_spend_accounted")
    if not isinstance(seen, list):
        seen = []
        state.metadata["_mission_spend_accounted"] = seen
    return seen


def _account_attempt_spend(
    state: ProductionState,
    shot: Shot,
    layer: Any,
    run_key: str,
    attempt_no: int,
    result: dict[str, Any],
) -> float | None:
    """Acumula el gasto REPORTADO de UN intento de capa, una sola vez.

    Devuelve el gasto contabilizado; None si el runner no reportó un coste
    finito no negativo, o si el intento ya estaba contabilizado (replay tras
    corte). Acumula en `shot.cost_spent_usd` (gasto del shot entre escalones)
    y en `state.cost_spent_usd` (gasto de la MISIÓN: todos los shots, todos
    los escalones); fija `spend_reported` en ambos — es un hecho medido. Los
    replans/reintentos NUNCA reinician ninguno de los dos: el gasto consumido
    es pasado.
    """
    try:
        spent = float(result["cost_usd"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(spent) or spent < 0:
        return None
    # identidad del intento: identidad de op (idempotency_key) + ordinal de
    # reintento in-place (los reintentos transient comparten run_key).
    attempt_key = f"{run_key}#{attempt_no}"
    seen = _mission_spend_seen(state)
    if attempt_key in seen:
        return None
    seen.append(attempt_key)
    shot.cost_spent_usd = round(shot.cost_spent_usd + spent, 6)
    shot.spend_reported = True
    state.cost_spent_usd = round(state.cost_spent_usd + spent, 6)
    state.spend_reported = True
    state.record(
        "layer_spend",
        shot=shot.id,
        layer=layer.kind,
        engine=layer.engine,
        tier=str(shot.replan_tier) if shot.replan_tier else None,
        spent_usd=spent,
        cumulative_usd=shot.cost_spent_usd,
        mission_cumulative_usd=state.cost_spent_usd,
    )
    return spent


def _restore_attempt_ordinal(
    active: Any, shot: Shot, layer_index: int, run_key: str
) -> int:
    """Ordinal de reintento in-place PERSISTIDO para esta capa, o 0.

    La identidad del intento es `run_key#ordinal`; el ordinal vive en
    `_active_layer`, que viaja en el checkpoint. Sin restaurarlo, un replay
    tras corte reiniciaría el ordinal a 0 y colisionaría con el intento ya
    contabilizado: el gasto del intento en vuelo se perdería (under-count).
    Con él, la identidad es estable y el accounting es exacto.
    """
    if not isinstance(active, dict):
        return 0
    if (
        active.get("shot_id") != shot.id
        or active.get("layer_index") != layer_index
        or active.get("idempotency_key") != run_key
    ):
        return 0
    try:
        return max(0, int(active.get("transient_retries", 0)))
    except (TypeError, ValueError):
        return 0


def _artifact_problem(path: str | None) -> str | None:
    """Razón por la que un artefacto declarado NO es utilizable, o None si vale.

    Un artefacto vale si existe, es un fichero regular y tiene bytes (>0).
    """
    if not path:
        return None
    p = Path(path)
    try:
        if not p.is_file():
            return "missing"
        if p.stat().st_size <= 0:
            return "empty"
    except OSError:
        return "missing"
    return None


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
                problem = _artifact_problem(layer.output_path)
                if problem is None:
                    last_output = layer.output_path
                    continue
                # Artefacto del checkpoint ya no está en disco (volumen efímero,
                # limpieza, crash antes del flush): reusarlo ensamblaría una
                # ruta colgante en el reel y en la galería. Se descarta el
                # estado del intento y se re-ejecuta la capa con identidad de
                # operación fresca (el idempotency_key incluye replan_attempts),
                # SIN consumir escalón de replan: perder el fichero no es un
                # fallo del plan.
                state.record(
                    "stale_artifact",
                    shot=shot.id,
                    layer=layer.kind,
                    engine=layer.engine,
                    path=layer.output_path,
                    problem=problem,
                )
                layer.output_path = None
                shot.output_path = None
                invalidate_shot_lineage(state, shot)
                shot.replan_attempts += 1
                # fall through: la capa se re-ejecuta más abajo.
            key_material = (
                f"{state.id}\0{shot.id}\0{layer_index}\0{shot.replan_attempts}"
            )
            run_key = "prod_" + hashlib.sha256(key_material.encode()).hexdigest()[:40]
            # identidad de intento estable: retomar el ordinal de reintento
            # persistido ANTES de sobrescribir `_active_layer` (replay tras
            # corte a mitad de los reintentos in-place).
            transient_retries = _restore_attempt_ordinal(
                state.metadata.get("_active_layer"), shot, layer_index, run_key
            )
            state.metadata["_active_layer"] = {
                "shot_id": shot.id,
                "layer_index": layer_index,
                "idempotency_key": run_key,
                "transient_retries": transient_retries,
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
            # P1 (misión): sin presupuesto RESTANTE de misión no hay receta
            # siguiente factible que comprar — ni para este shot ni para
            # ningún otro. Termina tipado ANTES de invocar cualquier runner
            # (fail-closed, igual que el gate de shot: 0 como cost_cap es
            # ambiguo aguas abajo).
            if _mission_budget_exhausted(state):
                _mark_mission_budget_exhausted(
                    state, shot, trigger="layer_dispatch"
                )
                return state
            # P1: el gasto es CUMULATIVO entre intentos. El cap que se arma en
            # cada intento es el RESTANTE (cap - gastado), no el original: un
            # shot que ya gastó en escalones previos no puede volver a disponer
            # del cap completo. Sin cap (0) sigue siendo None = sin tope.
            # fail-closed ANTES de invocar cualquier runner: agotado el cap
            # de shot no se llama con cost_cap=0, porque 0 puede interpretarse
            # como "sin tope" (P1).
            if shot.cost_cap_usd > 0 and shot.cost_spent_usd >= shot.cost_cap_usd:
                tier = shot.escalate_replan()
                invalidate_shot_lineage(state, shot)
                _record_budget_exhausted(state, shot, layer, tier)
                return state
            cost_cap = _remaining_attempt_cap(state, shot)
            # Fallos TRANSIENT se reintentan in-place (misma identidad de op,
            # la operación nunca completó) con presupuesto acotado; agotado,
            # escala la escalera como cualquier otro fallo. INVALID_PARAMETERS
            # no quema reintentos idénticos; PERMISSION_DENIED falla tipado sin
            # tocar la escalera. Un resultado legacy (sin clase) se comporta
            # exactamente como antes.
            transient_retries = 0
            while True:
                # Recompute the allowance for every in-place retry. A runner
                # can report spend on a transient failure; passing the original
                # cap again would let the next attempt exceed the cumulative
                # shot budget.
                cost_cap = _remaining_attempt_cap(state, shot)
                result = run.run_layer(
                    layer.engine,
                    job,
                    cost_cap=cost_cap,
                    cancel_event=cancel_event,
                )
                # el accounting es honesto: un run fallido también puede haber
                # consumido gasto; se acumula si el runner lo reporta. P1: el
                # mismo intento se contabiliza UNA sola vez en shot y MISIÓN;
                # un replay tras corte que re-obtenga el resultado deduplicado
                # del runner no lo vuelve a sumar.
                spent = _account_attempt_spend(
                    state, shot, layer, run_key, transient_retries, result
                )
                if spent is not None and checkpoint is not None:
                    # persistir el gasto contabilizado ANTES de decidir el
                    # siguiente paso (reintento in-place / escalón): un corte
                    # aquí no puede perder ni duplicar el gasto del intento.
                    checkpoint(state)
                if result.get("ok", False):
                    break
                failure_kind = classify_runner_failure(result)
                if failure_kind is LayerFailureKind.PERMISSION_DENIED:
                    # Un permiso denegado no lo arregla ni un reintento ni un
                    # replan (los escalones no cambian autorizaciones): fallo
                    # terminal tipado, sin consumir la escalera.
                    shot.error = (
                        f"[permission_denied] "
                        f"{str(result.get('error', 'permiso denegado sin detalle'))[:480]}"
                    )
                    shot.status = ShotStatus.FAILED
                    invalidate_shot_lineage(state, shot)
                    state.record(
                        "permission_denied",
                        shot=shot.id,
                        layer=layer.kind,
                        engine=layer.engine,
                    )
                    return state
                if (
                    failure_kind is LayerFailureKind.TRANSIENT
                    and transient_retries < MAX_TRANSIENT_RETRIES
                ):
                    # P1: el gasto del intento fallido ya se acumuló; el
                    # reintento solo procede si el cap RESTANTE lo permite.
                    if shot.cost_cap_usd > 0 and shot.cost_spent_usd >= shot.cost_cap_usd:
                        tier = shot.escalate_replan()
                        invalidate_shot_lineage(state, shot)
                        _record_budget_exhausted(state, shot, layer, tier)
                        return state
                    if _mission_budget_exhausted(state):
                        # el gasto del intento fallido agotó la MISIÓN: no hay
                        # receta siguiente factible que financiar el reintento.
                        _mark_mission_budget_exhausted(
                            state, shot, trigger="transient_retry"
                        )
                        return state
                    transient_retries += 1
                    # persistir el ordinal del PRÓXIMO intento antes de
                    # re-invocar: un corte antes de su accounting no pierde la
                    # identidad del intento en vuelo (ni gasto ni duplicado).
                    active_layer = state.metadata.get("_active_layer")
                    if isinstance(active_layer, dict):
                        active_layer["transient_retries"] = transient_retries
                    state.record(
                        "layer_transient_retry",
                        shot=shot.id,
                        layer=layer.kind,
                        engine=layer.engine,
                        attempt=transient_retries,
                        max_retries=MAX_TRANSIENT_RETRIES,
                        failure_kind=str(failure_kind),
                    )
                    if checkpoint is not None:
                        checkpoint(state)
                    continue
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
                    failure_kind=str(failure_kind),
                )
                return state
            layer.output_path = result.get("output_path")
            last_output = layer.output_path
            if result.get("simulated"):
                state.metadata["simulated"] = True
                state.metadata["execution_mode"] = "simulated"
                state.record(
                    "simulation_boundary",
                    shot=shot.id,
                    layer=layer.kind,
                    engine=layer.engine,
                )
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
