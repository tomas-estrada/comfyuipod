"""Recuperación de jobs interrumpidos tras reinicio del servidor (M3-B, tarea 14).

Los jobs persisten en SQLite; si el servidor (o el contenedor) cae con trabajos
en vuelo, al arrancar de nuevo quedan en estado `queued`/`running` sin ningún
ejecutor vivo. Este módulo:

1. Marca esos jobs huérfanos como `interrupted`.
2. Con `STUDIO_RESUME_JOBS=1` (por defecto), reencola UNA vez los jobs
   `kind="render"` con plantilla y proyecto vivos, reconstruyendo el workflow
   desde los parámetros validados persistidos (las entradas ya subidas a
   ComfyUI siguen siendo válidas porque `input/` vive en `COMFY_DATA_DIR`).
3. El contador `interrupt_retries` en `result_json` acota el reintento
   automático a `MAX_INTERRUPT_RETRIES` por job (nunca bucles infinitos).
4. Con `STUDIO_RESUME_JOBS=0` la recuperación es inerte: no se altera el estado
   de ningún job (ni marca `interrupted` ni reencola).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .comfyui_client import ComfyUIClient, ComfyUIError
from .progress import ProgressBroker
from .projects import ProjectStore
from .templates import get_default_registry

logger = logging.getLogger("comfy_studio.recovery")

STALE_STATUSES = ("queued", "running", "cancelling", "fetching")
MAX_INTERRUPT_RETRIES = 1
MAX_CONCURRENT_RESUMES = 2  # M3-SCR-016: reanudaciones simultáneas en el arranque


def find_stale_jobs(store: ProjectStore) -> list[dict[str, Any]]:
    """Jobs en estados en vuelo que ningún ejecutor posee tras un reinicio."""
    stale: list[dict[str, Any]] = []
    for status in STALE_STATUSES:
        stale.extend(store.list_jobs(status=status))
    return stale


async def recover_interrupted_jobs(
    *,
    store: ProjectStore,
    comfyui_client: ComfyUIClient,
    broker: ProgressBroker | None = None,
    resume: bool = True,
    start_delay_s: float = 0.0,
) -> dict[str, int]:
    """Marca jobs huérfanos como `interrupted` y reencola una vez los elegibles.

    Devuelve un informe `{"stale", "interrupted", "requeued", "done", "failed"}`.
    Con `resume=False` (`STUDIO_RESUME_JOBS=0`) la recuperación es **inerte**: no
    se altera el estado de ningún job (ni se marca `interrupted` ni se reencola);
    el operador decidió desactivarla y la reconciliación es manual.
    Los fallos de conexión con ComfyUI no propagan: el job queda en estado
    terminal con la causa en `error` (el arranque del servidor nunca bloquea).
    """
    stale = find_stale_jobs(store)
    report = {"stale": len(stale), "interrupted": 0, "requeued": 0, "done": 0, "failed": 0}
    if not stale:
        return report

    if not resume:
        logger.info(
            "STUDIO_RESUME_JOBS=0: recuperación inerte para %d job(s) en vuelo.", len(stale)
        )
        return report

    if start_delay_s > 0:
        await asyncio.sleep(start_delay_s)

    registry = get_default_registry()
    resume_semaphore = asyncio.Semaphore(MAX_CONCURRENT_RESUMES)
    pending_executions: list[dict[str, Any]] = []
    for job in stale:
        job_id = str(job["id"])
        project_id = job.get("project_id")
        template_id = job.get("template_id")
        result_meta = dict(job.get("result") or {})
        retries = int(result_meta.get("interrupt_retries") or 0)
        project_alive = bool(project_id) and store.get_project(str(project_id)) is not None

        can_resume = (
            job.get("kind") == "render"
            and bool(template_id)
            and project_alive
            and retries < MAX_INTERRUPT_RETRIES
        )

        if not can_resume:
            reason = (
                "Job interrumpido por reinicio del servidor; reintentos automáticos agotados."
                if retries >= MAX_INTERRUPT_RETRIES and job.get("kind") == "render"
                else "Job interrumpido por reinicio del servidor."
            )
            store.update_job(
                job_id,
                status="interrupted",
                result={**result_meta, "interrupt_retries": retries},
                error=reason,
            )
            report["interrupted"] += 1
            if broker is not None:
                broker.publish(
                    "job",
                    {
                        "job_id": job_id,
                        "project_id": project_id,
                        "status": "interrupted",
                    },
                )
            continue

        try:
            workflow = registry.render_workflow(
                str(template_id),
                dict(job.get("params") or {}),
                project_id=str(project_id),
                job_id=job_id,
                strict=False,
            )
        except Exception as exc:
            store.update_job(
                job_id,
                status="interrupted",
                result={**result_meta, "interrupt_retries": retries},
                error=f"No se pudo reconstruir el workflow tras reinicio: {exc}",
            )
            report["failed"] += 1
            continue

        store.update_job(
            job_id,
            status="queued",
            result={
                **result_meta,
                "interrupt_retries": retries + 1,
                "resumed_after_restart": True,
            },
            error="",
        )
        if broker is not None:
            broker.publish(
                "job",
                {
                    "job_id": job_id,
                    "project_id": project_id,
                    "status": "queued",
                    "resumed": True,
                },
            )
        report["requeued"] += 1
        pending_executions.append(
            {
                "job_id": job_id,
                "workflow": workflow,
                "project_id": str(project_id),
                "template_id": str(template_id),
            }
        )

    # M3-SCR-016: ejecutar las reanudaciones con concurrencia acotada en lugar de
    # esperar cada render secuencialmente (con N huérfanos, la recuperación total era
    # la suma de todas las esperas y bloqueaba el arranque de los primeros jobs).
    async def _resume_one(exec_spec: dict[str, Any]) -> str:
        async with resume_semaphore:
            exec_job_id = exec_spec["job_id"]
            try:
                await comfyui_client.execute_prompt_job(
                    exec_spec["workflow"],
                    job_id=exec_job_id,
                    project_id=exec_spec["project_id"],
                    template_id=exec_spec["template_id"],
                )
                return "done"
            except ComfyUIError as exc:
                # execute_prompt_job ya marcó el job como failed vía _fail_job
                logger.warning("Reanudación del job %s falló: %s", exec_job_id, exc)
                return "failed"
            except Exception as exc:
                store.update_job(
                    exec_job_id,
                    status="interrupted",
                    error=f"Reanudación fallida tras reinicio: {exc}",
                )
                logger.warning("Reanudación del job %s falló: %s", exec_job_id, exc)
                return "failed"

    if pending_executions:
        try:
            outcomes = await asyncio.gather(
                *(_resume_one(spec) for spec in pending_executions)
            )
        except asyncio.CancelledError:
            # M1.6: un shutdown a mitad de reanudación dejaría jobs en estado en vuelo sin
            # ejecutor. Se marcan `interrupted` explícitamente antes de propagar la
            # cancelación para que la reconciliación sea determinista.
            for spec in pending_executions:
                store.update_job(
                    spec["job_id"],
                    status="interrupted",
                    error="Reanudación interrumpida por el apagado del servidor.",
                )
            logger.warning(
                "Recuperación cancelada durante el apagado: %d job(s) marcados interrupted.",
                len(pending_executions),
            )
            raise
        for outcome in outcomes:
            report["done" if outcome == "done" else "failed"] += 1

    return report
