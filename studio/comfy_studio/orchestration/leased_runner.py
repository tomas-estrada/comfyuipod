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

from comfy_studio.engines.process_registry import (
    cleanup_process_groups,
    process_group_scope,
    process_groups_clean,
)
from comfy_studio.orchestration.gpu_lease import (
    GpuLease,
    LeaseAdmissionError,
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
            # Capa de CPU: valida el presupuesto de RAM sin tomar el lock GPU.
            requested_ram = job.get("lease_ram_mb", self._ram)
            if isinstance(requested_ram, bool) or not isinstance(requested_ram, int):
                return {
                    "ok": False,
                    "engine": engine,
                    "error": "lease_ram_mb debe ser entero",
                    "lease": "error",
                }
            try:
                self._lease.acquire(
                    owner=self._owner,
                    engine=engine,
                    vram_mb=0,
                    ram_mb=requested_ram,
                    heavy=False,
                )
            except LeaseError as exc:
                return {
                    "ok": False,
                    "engine": engine,
                    "error": f"admisión RAM rechazada: {exc}",
                    "lease": "error",
                }
            return self._inner.run_layer(
                engine, job, cost_cap=cost_cap, cancel_event=cancel_event
            )

        # Capa pesada: adquirir el heavy lease antes de tocar la GPU.
        try:
            requested_vram = job.get("lease_vram_mb", self._vram)
            requested_ram = job.get("lease_ram_mb", self._ram)
            if isinstance(requested_vram, bool) or not isinstance(requested_vram, int):
                raise LeaseAdmissionError("lease_vram_mb debe ser entero")
            if isinstance(requested_ram, bool) or not isinstance(requested_ram, int):
                raise LeaseAdmissionError("lease_ram_mb debe ser entero")
            grant = self._lease.acquire(
                owner=self._owner,
                engine=engine,
                vram_mb=requested_vram,
                ram_mb=requested_ram,
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

        process_dir = self._lease.child_process_dir(grant)

        def cleanup_verified() -> bool:
            if process_groups_clean(process_dir):
                return True
            return cleanup_process_groups(process_dir) and process_groups_clean(process_dir)

        try:
            with process_group_scope(process_dir):
                result = self._inner.run_layer(
                    engine, job, cost_cap=cost_cap, cancel_event=cancel_event
                )
        except BaseException as engine_error:
            # El lease se libera también cuando falla/cancela el motor. Si la
            # liberación o el unload fallan, no ocultarlo bajo la excepción
            # original: el caller debe saber que la GPU puede seguir ocupada.
            try:
                release = self._lease.release(
                    grant,
                    unload=self._unload,
                    cleanup_verified=cleanup_verified(),
                )
                cleanup_failed = bool(release.get("cleanup_failed"))
            except Exception:  # noqa: BLE001
                raise LeaseCleanupError(
                    f"{engine} failed and GPU lease cleanup also failed"
                ) from engine_error
            if cleanup_failed:
                raise LeaseCleanupError(
                    f"{engine} failed and GPU unload reported cleanup_failed"
                ) from engine_error
            raise

        try:
            release = self._lease.release(
                grant,
                unload=self._unload,
                cleanup_verified=cleanup_verified(),
            )
        except Exception as cleanup_error:  # noqa: BLE001
            return _cleanup_failure(engine, result, cleanup_error)
        if release.get("cleanup_failed"):
            return _cleanup_failure(engine, result)
        result.setdefault("lease", "released")
        return result


class LeaseCleanupError(RuntimeError):
    """Motor falló y el lease no se pudo liberar/limpiar con certeza."""


def _cleanup_failure(
    engine: str, result: Any, error: Exception | None = None
) -> dict[str, Any]:
    """Fail closed when engine work succeeded but GPU cleanup is uncertain."""
    if isinstance(result, dict):
        result["ok"] = False
        result["lease"] = "cleanup_failed"
        result["lease_cleanup_failed"] = True
        result["error"] = f"{engine} finished but GPU lease cleanup failed"
        if error is not None:
            result["cleanup_error_type"] = type(error).__name__
        return result
    failure = {
        "ok": False,
        "engine": engine,
        "lease": "cleanup_failed",
        "lease_cleanup_failed": True,
        "error": f"{engine} finished but GPU lease cleanup failed",
    }
    if error is not None:
        failure["cleanup_error_type"] = type(error).__name__
    return failure


__all__ = ["CPU_ENGINES", "LeaseCleanupError", "LeasedEngineRunner"]
