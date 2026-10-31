"""Configuración para el worker de arq con SQLite."""

from arq.connections import RedisSettings


async def startup(ctx):
    """Inicializa el worker."""
    pass


async def shutdown(ctx):
    """Limpia recursos al apagar el worker."""
    pass


class WorkerSettings:
    """Configuración del worker de arq."""
    
    functions = []
    redis_settings = RedisSettings(host="sqlite")
    queue_read_limit = 100
    max_jobs = 10
    job_timeout = 300
    keep_result = 60
    queue_strategy = "priority"  # Habilita priorización de jobs