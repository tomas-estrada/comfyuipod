"""Árbitro de lease global de GPU entre procesos (plan Ultra, W03).

El estudio ejecuta varios tipos de motor que compiten por una única GPU de
96 GB: ComfyUI, Blender, inferencia ML propia, audio, VLM y jueces pesados. La
cola interna de ComfyUI no sabe nada de Blender ni del juez; un `Semaphore` de
`threading`/`asyncio` solo coordina dentro de un proceso. La política (W03 /
acceptance A04) exige un árbitro **entre procesos**:

- un solo **heavy lease** a la vez (un stage GPU pesado); los stages de CPU
  siguen en paralelo si su presupuesto cabe, sin tocar CUDA;
- **owner + fencing token**: cada concesión lleva un token monótono; un owner
  antiguo no puede liberar ni pisar el lease de uno nuevo;
- **detección de huérfanos**: si el proceso dueño murió, su lease se puede
  recuperar; si sigue vivo, NO — no se reutiliza la GPU bajo sus pies;
- **admisión** por VRAM/RAM contra el perfil efectivo (no una env que inventa
  memoria: usa `hardware_detect`);
- **cancelación/timeout/excepción** liberan el lease y reportan estado, o
  `cleanup_failed` explícito;
- **unload comprobado**: al liberar se puede exigir una rutina de descarga de
  VRAM cuyo resultado se verifica.

Mecanismo: un directorio de lock en disco (`${COMFY_DATA_DIR}/.gpu-lease/`) con
creación atómica (`O_CREAT|O_EXCL`), que es visible a todos los procesos del
pod. No usa CUDA: A04 corre en CPU y los picos reales de VRAM se validan en A29
(campaña GPU). Portable: en Windows/macOS de desarrollo funciona igual para los
tests; el pod es Linux.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Un heavy lease sin heartbeat durante este tiempo se considera abandonado
#: (su proceso dueño además debe estar muerto para recuperarlo).
DEFAULT_STALE_SECONDS = 120.0

#: Nombre del directorio de lock, bajo COMFY_DATA_DIR para que lo vean todos los
#: procesos del pod (ComfyUI, Blender, workers ML) sobre el volumen compartido.
LOCK_DIRNAME = ".gpu-lease"


class LeaseError(RuntimeError):
    """Error de adquisición/liberación del lease."""


class LeaseUnavailable(LeaseError):
    """La GPU ya está tomada por un owner vivo."""


class LeaseAdmissionError(LeaseError):
    """La solicitud no cabe en el presupuesto de recursos."""


class FencingError(LeaseError):
    """Un owner antiguo intentó operar sobre un lease que ya no es suyo."""


def _pid_alive(pid: int) -> bool:
    """¿Sigue vivo el proceso `pid`? Portable (POSIX signal 0 / Windows)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        # Windows: OpenProcess vía ctypes; si abre, está vivo.
        import ctypes  # noqa: PLC0415

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        # Comprobar que no haya terminado ya (exit code 259 = STILL_ACTIVE).
        exit_code = ctypes.c_ulong()
        ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
            handle, ctypes.byref(exit_code)
        )
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        return bool(ok) and exit_code.value == 259
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Existe pero es de otro usuario: para nuestro propósito, vivo.
        return True
    return True


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    """Concesión de un heavy lease. El `token` es la prueba de propiedad."""

    owner: str
    token: int
    pid: int
    engine: str
    vram_mb: int
    ram_mb: int
    acquired_at: float

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "token": self.token,
            "pid": self.pid,
            "engine": self.engine,
            "vram_mb": self.vram_mb,
            "ram_mb": self.ram_mb,
            "acquired_at": self.acquired_at,
        }


@dataclass(slots=True)
class AdmissionBudget:
    """Techo de recursos contra el que se admite un heavy lease.

    Debe venir del hardware REAL (hardware_detect.effective_profile), no de una
    variable de entorno: una env no puede inventar VRAM/RAM (W02/A02).
    """

    vram_mb: int
    ram_mb: int


class GpuLease:
    """Árbitro de lease global de GPU, respaldado por un lock en disco."""

    def __init__(
        self,
        *,
        lock_dir: Path,
        budget: AdmissionBudget,
        stale_seconds: float = DEFAULT_STALE_SECONDS,
        now: Callable[[], float] = time.time,
        pid: int | None = None,
        recover_orphan: Callable[[LeaseGrant], bool] | None = None,
    ) -> None:
        self._lock_dir = Path(lock_dir)
        self._budget = budget
        self._stale = stale_seconds
        self._now = now
        self._pid = pid if pid is not None else os.getpid()
        self._recover_orphan = recover_orphan
        self._lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock_file = self._lock_dir / "heavy.lock"
        self._token_file = self._lock_dir / "token.seq"
        self._coordination_file = self._lock_dir / "coordination.lock"

    @contextmanager
    def _coordinate(self) -> Iterator[None]:
        """Serialize lease inspection, stale recovery, acquisition, and release.

        `O_EXCL` alone serializes creation, but not stale-lock removal: two
        processes could both read a dead owner and the second unlink the first
        process's newly acquired lock. An OS advisory lock closes that gap and
        is released automatically if its owner crashes.
        """
        fd = os.open(
            self._coordination_file,
            os.O_CREAT | os.O_RDWR,
            0o600,
        )
        locked = False
        try:
            if os.name == "nt":
                import msvcrt  # noqa: PLC0415

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                # LK_LOCK waits/retries for the byte-range lock and is released
                # by the OS if this process exits unexpectedly.
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                locked = True
            else:
                import fcntl  # noqa: PLC0415

                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            yield
        finally:
            try:
                if locked:
                    if os.name == "nt":
                        import msvcrt  # noqa: PLC0415

                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl  # noqa: PLC0415

                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    # -- introspección ----------------------------------------------------

    def current(self) -> LeaseGrant | None:
        """El grant activo, o None si no hay lock."""
        if not self._lock_file.exists():
            return None
        try:
            data = json.loads(self._lock_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return LeaseGrant(
            owner=data["owner"],
            token=int(data["token"]),
            pid=int(data["pid"]),
            engine=data.get("engine", "?"),
            vram_mb=int(data.get("vram_mb", 0)),
            ram_mb=int(data.get("ram_mb", 0)),
            acquired_at=float(data.get("acquired_at", 0.0)),
        )

    def _is_recoverable(self, grant: LeaseGrant) -> bool:
        """Require dead owner plus engine cleanup verification before recovery."""
        if _pid_alive(grant.pid) or self._recover_orphan is None:
            return False
        try:
            return bool(self._recover_orphan(grant))
        except Exception:  # noqa: BLE001 - uncertainty must quarantine the lease
            return False

    def child_process_dir(self, grant: LeaseGrant) -> Path:
        """Directory containing process-group markers for one fencing token."""
        return self._lock_dir / "children" / str(grant.token)

    def _next_token(self) -> int:
        """Token de fencing monótono, persistente entre procesos."""
        current = 0
        if self._token_file.exists():
            try:
                raw = self._token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise LeaseError("no se pudo leer la secuencia de fencing") from exc
            if not raw or not raw.isascii() or not raw.isdigit():
                raise LeaseError("secuencia de fencing ilegible; recuperación manual requerida")
            current = int(raw)
        nxt = current + 1
        # tmp único por proceso: aunque hoy solo el dueño del lock llama aquí,
        # un nombre por-PID evita que dos escrituras colisionen en el replace.
        tmp = self._token_file.with_suffix(f".seq.{self._pid}.tmp")
        tmp.write_text(str(nxt), encoding="utf-8")
        os.replace(tmp, self._token_file)
        return nxt

    # -- adquisición / liberación ----------------------------------------

    def acquire(
        self,
        *,
        owner: str,
        engine: str,
        vram_mb: int,
        ram_mb: int,
        heavy: bool = True,
    ) -> LeaseGrant | None:
        """Adquiere el heavy lease, o lanza si no se puede.

        - `heavy=False` es un stage de CPU: no toma el lock de GPU, solo se
          admite por RAM. Devuelve None (no hay token de GPU que devolver) y el
          llamador NO debe cargar CUDA.
        - admisión: la solicitud no puede exceder el presupuesto real.
        - exclusión: si hay un owner vivo, lanza `LeaseUnavailable`.
        """
        # Admisión de recursos (vale para heavy y CPU).
        if ram_mb > self._budget.ram_mb:
            raise LeaseAdmissionError(
                f"RAM pedida {ram_mb} MB > presupuesto {self._budget.ram_mb} MB"
            )
        if heavy and vram_mb > self._budget.vram_mb:
            raise LeaseAdmissionError(
                f"VRAM pedida {vram_mb} MB > presupuesto {self._budget.vram_mb} MB"
            )

        if not heavy:
            # Stage CPU: no compite por la GPU. No se crea lock ni se carga CUDA.
            return None

        with self._coordinate():
            existing = self.current()
            if existing is not None:
                if not self._is_recoverable(existing):
                    raise LeaseUnavailable(
                        f"GPU ocupada por owner={existing.owner} pid={existing.pid}; "
                        "cleanup del engine no verificado"
                    )
                # Owner muerto: recovery y creación del sucesor ocurren en la
                # misma sección crítica; otro proceso no puede borrar el nuevo.
                self._lock_file.unlink(missing_ok=True)
            elif self._lock_file.exists():
                # Un proceso muerto entre O_EXCL y la escritura deja un archivo
                # vacío. Bajo el coordinador ya no hay escritor vivo en vuelo.
                if self._lock_file.stat().st_size == 0:
                    self._lock_file.unlink(missing_ok=True)
                else:
                    raise LeaseUnavailable(
                        "payload del lease ilegible; recuperación manual requerida"
                    )

            # La sección crítica coordina además el token persistente. Se
            # conserva O_EXCL como protección frente a procesos de versiones
            # antiguas que no conozcan coordination.lock.
            try:
                fd = os.open(
                    str(self._lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY
                )
            except FileExistsError:
                raise LeaseUnavailable("GPU tomada en carrera por otro proceso") from None

            try:
                token = self._next_token()
                grant = LeaseGrant(
                    owner=owner,
                    token=token,
                    pid=self._pid,
                    engine=engine,
                    vram_mb=vram_mb,
                    ram_mb=ram_mb,
                    acquired_at=self._now(),
                )
                payload = json.dumps(grant.to_public_dict(), ensure_ascii=False)
                os.write(fd, payload.encode("utf-8"))
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                self._lock_file.unlink(missing_ok=True)
                raise
            else:
                os.close(fd)
            return grant

    def release(
        self,
        grant: LeaseGrant,
        *,
        unload: Callable[[], bool] | None = None,
        cleanup_verified: bool = True,
    ) -> dict[str, Any]:
        """Libera el lease del `grant`, con fencing y cleanup/unload comprobados.

        - fencing: si el lock actual no es de este token, lanza `FencingError`
          (un owner antiguo no libera el lease de uno nuevo).
        - unload: si se da, se ejecuta y su resultado se verifica; si devuelve
          False o lanza, se devuelve `cleanup_failed` y se conserva el lock para
          impedir que otro motor use una GPU cuyo estado no se conoce.
        - cleanup_verified=False conserva el lock y omite unload cuando queda un
          process group registrado que aún podría estar usando CUDA.
        """
        if grant is None:
            return {"released": False, "reason": "no_grant"}
        with self._coordinate():
            existing = self.current()
            if existing is None:
                return {"released": True, "reason": "already_free", "cleanup_failed": False}
            if existing.token != grant.token:
                raise FencingError(
                    f"fencing: lock actual token={existing.token} != grant token={grant.token}"
                )

            cleanup_failed = not cleanup_verified
            if cleanup_failed:
                return {
                    "released": False,
                    "reason": "cleanup_failed",
                    "cleanup_failed": True,
                    "token": grant.token,
                }
            if unload is not None:
                try:
                    ok = unload()
                    cleanup_failed = not bool(ok)
                except Exception:  # noqa: BLE001 - un unload roto no debe dejar el lock tomado
                    cleanup_failed = True

            if cleanup_failed:
                # No admitir otro motor si no se confirmó el unload: el proceso
                # puede seguir vivo con modelos asignados a VRAM. El mismo owner
                # puede reintentar release; si muere, recovery puede continuar.
                return {
                    "released": False,
                    "reason": "cleanup_failed",
                    "cleanup_failed": True,
                    "token": grant.token,
                }

            # Exclusión hasta finalizar unload y retirar el token actual.
            self._lock_file.unlink(missing_ok=True)
            try:
                self.child_process_dir(grant).rmdir()
                (self._lock_dir / "children").rmdir()
            except OSError:
                pass
            return {
                "released": True,
                "reason": "released",
                "cleanup_failed": cleanup_failed,
                "token": grant.token,
            }

    def force_break(self) -> bool:
        """Recover only after owner death and verified process/engine cleanup."""
        with self._coordinate():
            existing = self.current()
            if existing is None:
                return False
            if not self._is_recoverable(existing):
                return False
            self._lock_file.unlink(missing_ok=True)
            try:
                self.child_process_dir(existing).rmdir()
                (self._lock_dir / "children").rmdir()
            except OSError:
                pass
            return True


def _lock_root_writable(path: Path) -> bool:
    """True si `path` se puede crear y admite escritura.

    Fuera del pod (desarrollo/CI) `/workspace` puede no existir o el `/` raíz
    puede ser de solo lectura (SIP en macOS): en ese caso el lock no puede vivir
    ahí y hay que usar un directorio temporal.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def lock_dir_for(env: dict[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    explicit = (environment.get("COMFY_DATA_DIR") or "").strip()
    if explicit:
        # El operador fijó el volumen a propósito: se respeta tal cual, sin
        # sondear su escritura (un path no creado todavía sigue siendo el destino
        # correcto; quien falla es el mkdir de GpuLease, con un error claro).
        return Path(explicit) / LOCK_DIRNAME
    candidate = Path("/workspace/comfyui-data") / LOCK_DIRNAME
    if _lock_root_writable(candidate):
        return candidate
    # Fuera del pod (desarrollo/CI) /workspace puede no existir o el `/` raíz
    # puede ser de solo lectura (SIP en macOS): sin este respaldo `create_app`
    # en modo orquestación real falla con "[Errno 30] Read-only file system".
    import tempfile

    fallback_root = Path(tempfile.gettempdir()) / "comfy_studio_data"
    fallback_root.mkdir(parents=True, exist_ok=True)
    return fallback_root / LOCK_DIRNAME


__all__ = [
    "AdmissionBudget",
    "FencingError",
    "GpuLease",
    "LeaseAdmissionError",
    "LeaseError",
    "LeaseGrant",
    "LeaseUnavailable",
    "lock_dir_for",
]
