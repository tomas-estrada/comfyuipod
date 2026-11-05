"""Adquisición de activos de modelo bajo demanda (política de arquitectura).

El resolvedor de capacidades dice **qué falta**; este módulo lo trae usando la
**herramienta de descarga existente** (el CLI `hf`/`huggingface-cli`), con las
mismas reglas que `scripts/download-models.sh`:

  - reutiliza lo que ya está en disco (skip si el destino existe y no está vacío);
  - comprueba espacio libre por intento y falla cerrado si no se puede leer;
  - staging en el mismo filesystem que el destino, con `rename()` atómico;
  - 3 intentos con backoff 2s → 5s → 11s y timeout por intento;
  - distingue timeout (rc 124) de fallo real;
  - admite progreso y cancelación entre intentos.

No selecciona modelos ni decide políticas: ejecuta un plan que le dan.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: `hf download` cancelado por el usuario (128+SIGINT, convención de shells).
CANCELLED_RETURNCODE = 130
#: Cliente colgado: se mata el árbol y se reintenta con backoff.
TIMEOUT_RETURNCODE = 124

GiB = 1024**3

DEFAULT_HF_FETCH_TIMEOUT_S = 3600
DEFAULT_DISK_MARGIN_BYTES = 10 * GiB
DEFAULT_MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 5, 11)

#: Directorio de staging aislado: los parciales deben sobrevivir a la limpieza
#: de las fases 1/2/3 (`.hf_stage`, `.hf_stage_phase3`).
STAGE_DIRNAME = ".hf_stage_capability"


def resolve_hf_cli(env: dict[str, str] | None = None) -> str | None:
    """`HF_CLI` explícito, si no `hf` y finalmente `huggingface-cli`."""
    environment = os.environ if env is None else env
    explicit = (environment.get("HF_CLI") or "").strip()
    if explicit:
        return explicit
    for candidate in ("hf", "huggingface-cli"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def default_stage_dir(env: dict[str, str] | None = None) -> Path:
    environment = os.environ if env is None else env
    comfy_data = (environment.get("COMFY_DATA_DIR") or "").strip()
    if not comfy_data:
        comfy_data = "/workspace/comfyui-data"
    return Path(comfy_data) / "models" / STAGE_DIRNAME


@dataclass(slots=True)
class AcquiredEntry:
    filename: str
    dest_path: str
    status: str  # "skipped" | "downloaded" | "failed"
    bytes: int = 0
    attempts: int = 0
    error: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "dest_path": self.dest_path,
            "status": self.status,
            "bytes": self.bytes,
            "attempts": self.attempts,
            "error": self.error,
        }


@dataclass(slots=True)
class AcquisitionReport:
    entries: list[AcquiredEntry] = field(default_factory=list)
    hf_cli: str | None = None
    stage_dir: str = ""
    cancelled: bool = False

    @property
    def downloaded_bytes(self) -> int:
        return sum(e.bytes for e in self.entries if e.status == "downloaded")

    @property
    def failed(self) -> list[AcquiredEntry]:
        return [e for e in self.entries if e.status == "failed"]

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "hf_cli": self.hf_cli,
            "stage_dir": self.stage_dir,
            "cancelled": self.cancelled,
            "downloaded_bytes": self.downloaded_bytes,
            "failed": [e.filename for e in self.failed],
            "entries": [e.to_public_dict() for e in self.entries],
        }


def _move(src: Path, dest: Path) -> None:
    """Mueve `src` a `dest` de forma atómica, aunque estén en volúmenes distintos.

    `os.replace` falla con `EXDEV` si staging y destino están en filesystems
    diferentes, que es perfectamente posible (`custom_nodes/*` vive bajo
    `COMFY_APP_DIR`). El fallback antiguo copiaba **directamente sobre `dest`
    final**: si la copia fallaba a mitad (disco lleno, cancelación), dejaba un
    fichero parcial en el destino y había destruido la versión previa válida.

    Defecto P0 del plan Ultra (W01). La publicación debe ser transaccional:

      1. copiar a un temporal EN EL MISMO FILESYSTEM que `dest`;
      2. `flush` + `fsync` del contenido y del directorio;
      3. `os.replace` del temporal sobre `dest` (atómico, same-fs);
      4. si algo falla, borrar el temporal y dejar intacto el `dest` previo.

    Así una copia interrumpida nunca publica un parcial ni destruye lo que ya
    había.
    """
    try:
        os.replace(src, dest)
        return
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.EXDEV:
            raise

    # Temporal en el directorio de destino => mismo filesystem => replace atómico.
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.incoming-{os.getpid()}.tmp"
    try:
        with open(src, "rb") as reader, open(tmp, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        # Publicación atómica: hasta aquí `dest` previo sigue intacto.
        os.replace(tmp, dest)
    except BaseException:
        # Cualquier fallo (incluida cancelación): no publicar parcial, no tocar
        # el destino previo. Se limpia el temporal y se propaga el error.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    # fsync del directorio para que el rename sobreviva a un corte de energía.
    try:
        dir_fd = os.open(str(dest.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # En plataformas sin fsync de directorio (Windows) no es fatal.
        pass
    src.unlink(missing_ok=True)


def _free_bytes(path: Path) -> int | None:
    """Espacio libre real del filesystem que contiene `path` (None si ilegible)."""
    try:
        target = path if path.exists() else path.parent
        if not target.exists():
            target = Path("/")
        return shutil.disk_usage(target).free
    except OSError:
        return None


def _free_bytes_for(paths: list[Path]) -> int | None:
    """Espacio libre del filesystem más restrictivo de entre `paths`.

    La política (punto 1) pide comprobar «usable space on the actual
    filesystems involved» —en plural—. Staging y destino pueden estar en
    volúmenes distintos, y mirar solo uno permite quedarse sin espacio en el
    otro justo cuando ya se ha descargado el activo.
    """
    free: int | None = None
    for path in paths:
        value = _free_bytes(path)
        if value is None:
            return None
        free = value if free is None else min(free, value)
    return free


def acquire(
    needs: list[Any],
    *,
    env: dict[str, str] | None = None,
    hf_cli: str | None = None,
    stage_dir: Path | None = None,
    timeout_s: int | None = None,
    margin_bytes: int | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    progress: Callable[[str, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AcquisitionReport:
    """Descarga los activos que faltan. `needs` son `AssetNeed` del resolvedor."""
    environment = os.environ if env is None else env
    cli = hf_cli or resolve_hf_cli(env)
    stage = stage_dir or default_stage_dir(env)
    timeout = timeout_s or int(
        float(environment.get("HF_FETCH_TIMEOUT") or DEFAULT_HF_FETCH_TIMEOUT_S)
    )
    margin = margin_bytes or int(
        float(environment.get("HF_DISK_MARGIN_BYTES") or DEFAULT_DISK_MARGIN_BYTES)
    )
    guard_enabled = (environment.get("HF_DISK_GUARD") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )

    report = AcquisitionReport(hf_cli=cli, stage_dir=str(stage))
    if cli is None:
        for need in needs:
            report.entries.append(
                AcquiredEntry(
                    filename=need.filename,
                    dest_path=need.dest_path,
                    status="failed",
                    error="sin cliente de HuggingFace (hf/huggingface-cli) disponible",
                )
            )
        return report

    for need in needs:
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            break
        report.entries.append(
            _acquire_one(
                need,
                cli=cli,
                stage=stage,
                timeout=timeout,
                margin=margin,
                max_attempts=max_attempts,
                guard_enabled=guard_enabled,
                progress=progress,
                should_cancel=should_cancel,
            )
        )
    return report


def git_blob_sha1(path: Path) -> str:
    """SHA-1 del objeto blob de Git para el fichero en `path`.

    **No es el SHA-1 crudo de sus bytes.** Git antepone la cabecera
    ``blob <tamaño>\0`` antes de calcular el hash:

        sha1(b"blob " + str(len(content)) + b"\0" + content)

    Verificado contra la API de HuggingFace: para un fichero guardado en git (no
    LFS), el `X-Linked-Etag` que publica es exactamente este valor, no el SHA-1
    del contenido. Confundirlos hacía fallar la verificación de todos los activos
    con hash SHA-1.
    """
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(b"blob %d\x00" % size)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raw_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_integrity(path: Path, need: Any) -> str | None | bool:
    """Compara el hash del fichero con el que declara el manifiesto.

    Devuelve `True` si coincide (o si el manifiesto no publica hash), y un
    mensaje de error si no. Un activo sin hash se acepta: la política dice
    «where available», y exigirlo bloquearía activos que HuggingFace solo
    sirve por tamaño.

    **Semántica del hash, que no es la misma en los dos casos:**

    - `sha256` (64 hex) es el oid del objeto **LFS**, que sí es el SHA-256 crudo
      del contenido del fichero.
    - `sha1` (40 hex) es el OID del **blob de git**, que lleva la cabecera
      ``blob <tamaño>\0``. Calcular el SHA-1 crudo aquí haría fallar la
      verificación de todos los activos con hash SHA-1.

    Verificado contra la API de HuggingFace en vivo (ver
    `test_etag_is_the_git_blob_sha1_not_the_raw_one`).
    """
    expected_sha256 = getattr(need, "sha256", None)
    expected_sha1 = getattr(need, "sha1", None)
    if not expected_sha256 and not expected_sha1:
        return True

    if expected_sha256:
        actual = _raw_digest(path, "sha256")
        if actual == str(expected_sha256).lower():
            return True
        return (
            f"sha256 (LFS) esperado {expected_sha256}, obtenido {actual}"
        )

    expected = str(expected_sha1).lower()
    actual = git_blob_sha1(path)
    if actual == expected:
        return True
    # Se muestran los dos valores calculados: si alguien fijó alguna vez un
    # SHA-1 crudo, el mensaje lo dice en vez de degradarse a «no coincide».
    return (
        f"sha1 (blob de git) esperado {expected}, obtenido {actual} "
        f"(SHA-1 crudo de los bytes: {_raw_digest(path, 'sha1')})"
    )


def _terminate_tree(proc: subprocess.Popen[str]) -> None:
    """Mata el cliente y sus hijos.

    `hf download` delega en procesos hijos para las descargas: matar solo al
    padre dejaría el descargador vivo escribiendo en staging. Por eso se mata el
    grupo de procesos, pero **solo si el cliente lo lidera** (lo garantiza el
    `start_new_session=True` de `_run_hf_download`): un `killpg` sobre un grupo
    ajeno mataría procesos que no son nuestros, incluido el nuestro.

    Portabilidad: `os.getpgid`/`os.killpg` y `SIGKILL` solo existen en POSIX.
    En Windows no hay grupos de proceso POSIX ni `SIGKILL`; se cae a
    `terminate()`/`kill()` del propio `Popen`, que allí usa `TerminateProcess`.
    El pod de producción es Linux, pero el cliente debe poder ejecutarse (y
    probarse) en Windows/macOS sin reventar por un atributo ausente.
    """
    hard_sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    getpgid = getattr(os, "getpgid", None)
    killpg = getattr(os, "killpg", None)
    for sig in (signal.SIGTERM, hard_sig):
        pgid = None
        if getpgid is not None and killpg is not None:
            try:
                pgid = getpgid(proc.pid)
            except OSError:
                pgid = None
            if pgid is not None and pgid == proc.pid:
                try:
                    killpg(pgid, sig)
                except OSError:
                    pass
        try:
            proc.kill() if sig is hard_sig else proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_hf_download(
    argv: list[str],
    *,
    timeout: int,
    should_cancel: Callable[[], bool] | None,
) -> int:
    """`hf download` que se puede cancelar mientras descarga.

    `subprocess.run` bloquea hasta que el fichero está completo: con activos de
    20 GB, un cancelado no tendría efecto hasta el final. Aquí el cliente se
    lanza en su propia sesión y se sondea cada segundo, así que `should_cancel`
    surte efecto en ~1 s. La salida va a DEVNULL porque no se consume, y leerla
    por tubería podría bloquearse con una barra de progreso verbosa.
    """
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        try:
            proc.wait(timeout=1.0)
            return proc.returncode
        except subprocess.TimeoutExpired:
            pass
        if should_cancel is not None and should_cancel():
            _terminate_tree(proc)
            return CANCELLED_RETURNCODE
        if time.monotonic() >= deadline:
            _terminate_tree(proc)
            return TIMEOUT_RETURNCODE


def _acquire_one(    need: Any,
    *,
    cli: str,
    stage: Path,
    timeout: int,
    margin: int,
    max_attempts: int,
    guard_enabled: bool,
    progress: Callable[[str, str], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> AcquiredEntry:
    dest = Path(need.dest_path) if need.dest_path else None
    expected_bytes = int(need.bytes or 0)
    if dest is not None and dest.is_file() and dest.stat().st_size > 0:
        # Un fichero preexistente NO es «presente» por el solo hecho de existir
        # y no estar vacío. Un parcial, un fichero corrupto o uno sustituido
        # tienen tamaño > 0 y pasarían como adquiridos. La política (R15) exige
        # verificar tamaño e integridad antes de anunciar disponible; de lo
        # contrario un activo de 7 bytes frente a 26 esperados se reutilizaría.
        # Defecto P0 del plan Ultra (W01): caché corrupta aceptada sin verificar.
        actual_size = dest.stat().st_size
        has_hash = bool(getattr(need, "sha256", None) or getattr(need, "sha1", None))
        # Prioridad de verificación:
        #  - si el manifiesto declara hash, ese hash es la autoridad (subsume el
        #    tamaño): un contenido que casa el hash es el fichero correcto;
        #  - si NO hay hash («where available»), el tamaño exacto es la única
        #    señal disponible, así que un tamaño distinto descalifica el fichero.
        integrity = _verify_integrity(dest, need)
        if has_hash:
            size_ok = True  # el hash ya cubre el contenido completo
        else:
            size_ok = expected_bytes <= 0 or actual_size == expected_bytes
        if size_ok and integrity is True:
            if progress:
                progress(need.filename, "skipped")
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="skipped",
                bytes=actual_size,
            )
        # El fichero existente no verifica: se descarta y se vuelve a adquirir,
        # en vez de anunciarlo disponible. No se deja un corrupto en su sitio.
        detail = (
            str(integrity)
            if integrity is not True
            else f"tamaño {actual_size} != esperado {expected_bytes}"
        )
        if progress:
            progress(need.filename, "corrupt:redownload")
        try:
            dest.unlink()
        except OSError:
            # Si no se puede borrar, no se puede garantizar integridad: fail-closed.
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="failed",
                error=f"fichero existente no verificable y no borrable: {detail}",
            )

    if not need.repo_id or not need.remote_path or dest is None:
        return AcquiredEntry(
            filename=need.filename,
            dest_path=need.dest_path,
            status="failed",
            error="activo sin origen declarado en el manifiesto",
        )

    expected = int(need.bytes or 0)
    staged = stage / need.remote_path
    # Un destino relativo se resolvería contra el CWD y podría escribir dentro
    # del repositorio: solo se aceptan rutas absolutas.
    if not dest.is_absolute():
        return AcquiredEntry(
            filename=need.filename,
            dest_path=str(dest),
            status="failed",
            error=f"destino no absoluto (se rechaza por seguridad): {dest}",
        )
    backoff_index = 0

    for attempt in range(1, max_attempts + 1):
        if guard_enabled:
            # Staging y destino: pueden estar en filesystems distintos.
            free = _free_bytes_for([stage, dest.parent])
            if free is None:
                return AcquiredEntry(
                    filename=need.filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error="espacio libre ilegible (fail-closed)",
                )
            if free < expected + margin:
                return AcquiredEntry(
                    filename=need.filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error=(
                        f"espacio insuficiente: {free / GiB:.1f} GiB libres, "
                        f"se necesitan {(expected + margin) / GiB:.1f} GiB"
                    ),
                )

        if progress:
            progress(need.filename, f"downloading:{attempt}")
        argv = [
            cli,
            "download",
            str(need.repo_id),
            str(need.remote_path),
            "--repo-type",
            str(need.repo_type or "model"),
            "--local-dir",
            str(stage),
        ]
        # Pin inmutable: si el manifiesto declara una revisión (commit), TODAS
        # las rutas de descarga deben fijarla. Omitir `--revision` haría que el
        # CLI baje el contenido de `main`, que puede diferir del hash fijado y
        # fallaría la verificación de integridad después (o, peor, cambiaría en
        # silencio bajo nosotros). Es un defecto P0 del plan Ultra (W01).
        revision = getattr(need, "revision", None)
        if revision:
            argv += ["--revision", str(revision)]
        try:
            returncode = _run_hf_download(
                argv,
                timeout=timeout,
                should_cancel=should_cancel,
            )
        except OSError as exc:
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="failed",
                attempts=attempt,
                error=f"no se pudo ejecutar {cli}: {exc}",
            )

        if returncode == 0 and staged.is_file() and staged.stat().st_size > 0:
            # Integridad «where available»: el manifiesto publica el hash que
            # HuggingFace declara (SHA-256 del objeto LFS o SHA-1 del blob git).
            verified = _verify_integrity(staged, need)
            if verified is not True:
                staged.unlink(missing_ok=True)
                return AcquiredEntry(
                    filename=need.filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error=f"integridad no verificada: {verified}",
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            _move(staged, dest)
            if progress:
                progress(need.filename, "downloaded")
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="downloaded",
                bytes=dest.stat().st_size,
                attempts=attempt,
            )

        staged.unlink(missing_ok=True)
        if returncode == CANCELLED_RETURNCODE:
            # Cancelar no es un fallo: no se reintenta y el staging queda limpio
            # para que la recuperación no encuentre un fichero parcial.
            if progress:
                progress(need.filename, "cancelled")
            return AcquiredEntry(
                filename=need.filename,
                dest_path=str(dest),
                status="cancelled",
                attempts=attempt,
            )
        if attempt < max_attempts:
            delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
            backoff_index += 1
            time.sleep(delay)

    return AcquiredEntry(
        filename=need.filename,
        dest_path=str(dest),
        status="failed",
        attempts=max_attempts,
        error=(
            "timeout"
            if returncode == TIMEOUT_RETURNCODE
            else f"fallo del cliente (rc={returncode})"
        ),
    )
