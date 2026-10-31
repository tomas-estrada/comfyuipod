"""Definición de tareas para el worker de arq."""

from arq import create_pool
from arq.connections import RedisSettings


async def run_comfyui_job(ctx, job_data: dict):
    """Ejecuta un job de ComfyUI."""
    # Lógica para ejecutar el job
    print(f"Ejecutando job: {job_data}")
    return "Job completado"


async def enqueue_task(task_name: str, *args, **kwargs):
    """Encola una tarea en el worker de arq."""
    redis = await create_pool(RedisSettings(host="sqlite"))
    await redis.enqueue_job(task_name, *args, **kwargs)