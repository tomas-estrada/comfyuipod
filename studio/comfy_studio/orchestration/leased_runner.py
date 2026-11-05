"""Runner con lease de GPU para el executor (plan Ultra, W03↔W08 integración).

El executor (fase 14) ejecuta capas a través de un `EngineRunner`. El lease
global de GPU (W03) existe pero no estaba enganchado al executor: cada capa
pesada tocaba la GPU sin pasar por el árbitro. Este decorador cierra esa brecha.

`LeasedEngineRunner` envuelve otro `EngineRunner` y, para cada capa:

- decide si la capa es **pesada** (usa GPU) o de **CPU** según el motor;
- para una capa pesada, **adquiere el heavy lease** (W03) antes de ejecutar y lo
  **libera** después con fencing y unload comprobado — incluso si la ejecución
  lanza o falla (nunca deja la GPU bloqueada);
- una capa de CPU pasa directo, sin tomar el lock ni cargar CUDA;
- si la GPU está tomada por un owner vivo, devuelve un resultado tipado
  `ok=False` con el motivo, en vez de pisar el lease de otro.

No ejecuta nada en GPU por sí mismo: delega en el runner envuelto (stub en CI,
adaptador real en el pod) y solo administra el lease alrededor. Verificable en
CPU (A04); la generación real es del runner envuelto.
"""

from __future__ import annotations

from threading import Event
from typing import Any

from comfy_studio.orchestration.gpu_lease import (
    GpuLease,
    LeaseError,
    LeaseUnavailable,
)

#: Motores que NO usan GPU: sus capas no toman el heavy lease.
CPU_ENGINES = frozenset({"editor", "ffmpeg", "compositor", "asr"})


def _is_heavy(engine: str) -> bool:
    """¿La capa de este motor compite por la GPU?"""
    return engine not in CPU_ENGINES


class LeasedEngineRunner:
    """`EngineRunner` que administra el lease de GPU alrededor de cada capa."""

    def __init__(
        self,
        inner: Any,
        lease: GpuLease,
        *,
        owner: str = "orchestration-executor",
        vram_per_layer_mb: int = 40000,
        ram_per_layer_mb: int = 16000,
        unload: Any = None,
    ) -> None:
        self._inner = inner
        self._lease = lease
        self._owner = owner
        self._vram = vram_per_layer_mb
        self._ram = ram_per_layer_mb
        self._unload = unload

    def run_layer(
        self,
        engine: str,
        job: dict[str, Any],
        *,
        cost_cap: float | None = None,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        if not _is_heavy(engine):
            # Capa de CPU: no toma el lock de GPU. Se ejecuta directo.
            return self._inner.run_layer(
                engine, job, cost_cap=cost_cap, cancel_event=cancel_event
            )

        # Capa pesada: adquirir el heavy lease antes de tocar la GPU.
        try:
            grant = self._lease.acquire(
                owner=self._owner,
                engine=engine,
                vram_mb=self._vram,
                ram_mb=self._ram,
                heavy=True,
            )
        except LeaseUnavailable as exc:
            return {
                "ok": False,
                "engine": engine,
                "error": f"GPU ocupada: {exc}",
                "lease": "unavailable",
            }
        except LeaseError as exc:
            return {
                "ok": False,
                "engine": engine,
                "error": f"lease rechazado: {exc}",
                "lease": "error",
            }

        result: dict[str, Any] | None = None
        try:
            result = self._inner.run_layer(
                engine, job, cost_cap=cost_cap, cancel_event=cancel_event
            )
        finally:
            # El lease se libera SIEMPRE, incluso si la ejecución lanzó: no dejar
            # la GPU bloqueada. El unload (si se dio) se ejecuta y verifica aquí.
            # Si el runner envuelto lanzó, `result` es None y la excepción se
            # propaga tras liberar — pero la GPU queda libre pase lo que pase.
            try:
                release = self._lease.release(grant, unload=self._unload)
                if isinstance(result, dict):
                    result.setdefault("lease", "released")
                    if release.get("cleanup_failed"):
                        result["lease_cleanup_failed"] = True
            except LeaseError:
                # Un fencing error al liberar no debe enmascarar el resultado del
                # render; se anota pero no se lanza.
                if isinstance(result, dict):
                    result["lease"] = "release_fencing_error"
        return result


__all__ = ["CPU_ENGINES", "LeasedEngineRunner"]
