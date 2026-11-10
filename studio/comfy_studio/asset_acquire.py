"""Adquisición de activos de modelo bajo demanda (política de arquitectura).

El resolvedor de capacidades dice **qué falta**; este módulo lo trae usando la
**herramienta de descarga existente** (el CLI `hf`/`huggingface-cli`), con las
mismas reglas que `scripts/download-models.sh`:

  - reutiliza lo que ya está en disco, pero solo si sigue siendo el activo que
    declara el manifiesto (ver «ningún parcial» más abajo);
  - comprueba espacio libre antes de descargar y falla cerrado si no se puede
    leer (una vez descargado ya verificado, lo que queda es instalar);
  - staging en el mismo filesystem que el destino, con `rename()` atómico;
  - cada `acquire()` descarga bajo su propio subárbol de staging
    (`stage/job-<pid>-<seq>`, que es también el `--local-dir` del cliente), así
    que la pertenencia de un parcial es un prefijo de ruta y no un nombre: lo
    que otra adquisición tiene en vuelo es un hermano y la limpieza no lo
    recorre (D1);
  - 3 intentos con backoff 2s → 5s → 11s y timeout por intento;
  - un fallo al **instalar** reintenta el `rename` sin volver a descargar: la
    descarga ya verificada no se tira por un `rename` transitorio;
  - medir el destino **después** de instalar no es instalar: si el disco falla
    ahí el activo ya está publicado, así que se informa el tamaño **del
    stageado** —el que sí se midió, antes del `rename`— en lugar de reintentar
    el `rename` ni volver a bajarlo (R72). No es el tamaño del destino, que es
    el que no se pudo leer: es el del stageado del que salió, y la entrada lo
    dice en `error`;
  - distingue timeout (rc 124) de fallo real;
  - admite progreso y cancelación entre intentos.

Ningún parcial puede parecer completo (el invariante que gobierna todo lo de
arriba): el destino solo se reutiliza si sigue siendo el activo que declara el
manifiesto. Cuando el manifiesto publica hash, es el hash el que manda; cuando
solo publica tamaño, el tamaño. Hay un **tercer caso** que hay que nombrar
porque es el más restrictivo de los tres: cuando el manifiesto no declara ni
hash ni tamaño utilizable no hay nada con lo que comparar, así que el destino
**no se reutiliza** y el stageado **no se publica** —ni se adquiere, porque
bajarlo no arreglaría nada—. Sin ninguna de las dos redes no se puede afirmar
que un activo esté completo, así que se falla cerrado (R91,
`UNVERIFIABLE`). Hasear un activo de 20 GB en cada `acquire` no
es aceptable, así que la verificación se cachea en un sidecar junto al destino
(`<dest>.integrity`) indexado por `(mtime, size)`: si ninguno de los dos cambió,
el contenido es el que este módulo ya comprobó y no se vuelve a hasear. Esa
cache es una optimización, no una segunda verificación: quien restaure el
`mtime` a propósito (`cp -p`, `rsync -a`, un restore) con el mismo tamaño la
engañará, y eso está documentado en `_integrity_cache_matches`. Un destino que
no se deja medir deja su marca en `<dest>.installed`, para que un activo
instalado y verificado no se baje en cada pasada (R76, R77); la marca registra
el hash que el manifiesto declara y, sin hash, el tamaño declarado, así que una
revisión que solo cambia los bytes no reutiliza el activo anterior (R96, D3).

El directorio de destino es compartido, pero cada temporal de instalación lleva
el pid y la secuencia de su dueño. El barrido conserva siempre temporales de
procesos vivos, aunque el mtime sea antiguo: una copia cross-device lenta o
bloqueada no es huérfana por superar un umbral de edad. Cuando el proceso dueño
ya no existe, el temporal se puede retirar; para parciales sin pid se aplica
`STALE_PARTIAL_AGE_S`.

Variables de entorno que este módulo lee:

  - `HF_CLI`: cliente de HuggingFace a usar (ver `resolve_hf_cli`).
  - `HF_FETCH_TIMEOUT`: segundos por intento de descarga (3600 por defecto).
  - `HF_DISK_MARGIN_BYTES`: margen libre exigido además de los bytes del activo
    (10 GiB por defecto).
  - `HF_DISK_GUARD`: `0`/`false`/`no` **desactiva** la guarda de espacio, que
    por defecto falla cerrada antes de descargar. Existe para los entornos que
    miden el volumen por su cuenta (así la configuran las pruebas, que trabajan
    sobre `tmp_path`); en producción se deja en su valor por defecto, porque sin
    guarda una descarga de 20 GB puede agotar el disco (R83).

No selecciona modelos ni decide políticas: ejecuta un plan que le dan.
"""

from __future__ import annotations

import errno
import glob
import hashlib
import itertools
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
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

LOG = logging.getLogger(__name__)

#: Marca de los temporales de instalación: `<dest>.partial-<pid>-<seq>`.
PARTIAL_MARK = ".partial-"
#: Sidecar que fija la integridad ya comprobada de un destino:
#: `<dest>.integrity`, con el hash del manifiesto y el `(mtime, size)` sobre el
#: que se calculó. Sin él, cada `acquire` hasearía 20 GB para llegar al mismo
#: «ya está en disco» (R76).
INTEGRITY_SUFFIX = ".integrity"
#: Sidecar que registra un destino instalado y verificado que no se deja medir:
#: `<dest>.installed`. Existe para que un destino cuyo `stat()` falla siempre
#: (EIO/ESTALE en FUSE/NFS/SMB) no se baje en cada adquisición (R77). No es
#: prueba suficiente por sí sola: si el disco tampoco deja confirmar que la
#: entrada sigue en su directorio, se falla cerrado y se redescarga (R86).
INSTALLED_SUFFIX = ".installed"
#: Edad a partir de la cual un temporal abandonado se considera huérfano. Un
#: temporal más joven se respeta siempre —con proceso vivo o sin pid que
#: preguntar—: puede ser una copia en vuelo de otro `acquire` concurrente sobre
#: el mismo directorio, y borrarla haría fallar su `rename`.
STALE_PARTIAL_AGE_S = 3600.0

#: R91: motivo con el que se falla cerrado cuando el manifiesto no declara ni
#: hash ni tamaño utilizable. Sin ninguna de las dos redes no hay forma de
#: distinguir un activo completo de un parcial, así que no se publica ninguno.
UNVERIFIABLE = "integridad no verificada: el manifiesto no declara hash ni tamano"

#: Secuencia por proceso: dos temporales del mismo pid nunca comparten nombre.
#: `next()` sobre un `itertools.count()` es una sola operación atómica en
#: CPython; un `contador += 1` no lo es (lee, suma y escribe), y `acquire` corre
#: en un threadpool, así que dos adquisiciones concurrentes del mismo destino
#: podían acabar publicando el mismo temporal (R39).
_partial_seq = itertools.count()

#: R97: campos de la necesidad que acaban en una ruta. Además de los caracteres
#: de control, tienen que ser una ruta de verdad: `Path()` sobre una lista, un
#: diccionario o un entero responde `TypeError`, que ningún `except (OSError,
#: ValueError)` del módulo ve, así que la excepción salía de `acquire()`.
_PATH_FIELDS = frozenset({"dest_path"})


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
    #: `"skipped"` cubre dos situaciones que el llamador distingue por `bytes` y
    #: `error`: «ya está en disco, no se descarga» (`bytes>0`, `error=None`) y
    #: «no se intentó porque la adquisición se canceló» (`bytes=0`, `attempts=0`
    #: y `error` autoexplicativo). Se mantiene el vocabulario para no romper a
    #: quien ya cuenta por estado; el detalle vive en `error` (R23).
    status: str  # "skipped" | "downloaded" | "failed" | "cancelled"
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


def _fsync_dir(path: Path) -> None:
    """Fuerza a disco la entrada de `path` tras un `rename`.

    Sin esto, un corte de energía justo después del `os.replace` podría
    publicar el nombre nuevo pero no su contenido en algunos filesystems. Falla
    en silencio: en un directorio no siempre se puede hacer `fsync` y no es
    razón para abortar la adquisición.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except (OSError, ValueError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _fsync_file(path: Path) -> None:
    """Fuerza a disco el **contenido** de `path` antes de publicar su nombre.

    R82: en el camino de mismo filesystem, `os.replace` ya es atómico y
    `_fsync_dir` deja la entrada del directorio en disco, pero los bytes los
    escribió el cliente `hf` y este módulo no los había forzado. En un
    filesystem que reordena escrituras, un corte de luz justo después del rename
    podía publicar el nombre nuevo con contenido incompleto —exactamente lo que
    el docstring de `_fsync_dir` dice querer evitar, y que sincronizar solo el
    directorio no consigue—. El camino EXDEV sí lo hacía (`_copy_and_fsync`).

    Falla en silencio, igual que `_fsync_dir`: hay filesystems que no aceptan
    `fsync` sobre un descriptor de solo lectura, y eso no es razón para perder
    una instalación que ya está publicada. Se anota a `debug`.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo abrir %s para sincronizarlo: %s", path, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:
        LOG.debug("no se pudo forzar %s a disco: %s", path, exc)
    finally:
        os.close(fd)


def _copy_and_fsync(src: Path, dst: Path) -> None:
    """Copia `src` en `dst` y fuerza el contenido a disco antes de publicarlo."""
    with open(src, "rb") as reader, open(dst, "wb") as writer:
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _unlink_quietly(path: Path, *, warn: bool = False) -> None:
    """Borra `path` si se puede; un fallo se anota y no se propaga.

    La limpieza de parciales nunca puede abortar una adquisición: si lo hiciera,
    un staging sin permiso de escritura convertiría un cancelado en una
    excepción sin informe para el llamador. Con `warn=True` el fallo sube a
    `warning`, para los borrados cuyo éxito el informe da por hecho (R25).
    """
    try:
        path.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        if warn:
            LOG.warning("%s sigue en disco: %s", path, exc)
        else:
            LOG.debug("no se pudo borrar %s: %s", path, exc)


def _is_nameless_path(path: Path) -> bool:
    """True si borrar `path` podría llevarse el árbol entero por delante (R4).

    Un stageado sin nombre —`/`, `//`, `/..`, y `.`— no es un fichero: es la
    raíz del filesystem o el directorio actual. `_remove_staged` borra con
    `shutil.rmtree(..., ignore_errors=True)`, así que sobre esas rutas arrasaría
    el sistema **sin ruido**: ni excepción ni `warning` que lo delate. Es peor
    que el `ValueError` de D6, que al menos paraba el camino.

    Inalcanzable desde `acquire()` —la guarda del destino sin nombre y la
    validación de `remote_path` lo impiden—, pero `_remove_staged` la llaman
    también la limpieza de la caché del cliente y quien prueba los ayudantes
    directamente, y un borrado best-effort no puede ser destructivo por
    definición.
    """
    if path.name in ("", ".", ".."):
        return True
    try:
        resolved = path.resolve()
    except (OSError, ValueError):
        return True
    return len(resolved.parts) < 2 or resolved == Path(resolved.anchor)


def _remove_staged(staged: Path) -> None:
    """Borra el stageado, sea fichero o directorio, sin propagar nunca.

    Un `remote_path` de manifiesto sin extensión (`LICENSE`,
    `qwen0.6bemo4-merge/Modelfile`) puede materializarse en staging como
    **directorio**: un `unlink` sobre eso lanza `IsADirectoryError`, que con un
    `missing_ok=True` pelado salía de `acquire()` sin informe (R22, el mismo
    síntoma que R13). La limpieza best-effort existente ya protegía el
    `PermissionError`; aquí se cubre también la forma de directorio.

    R4: lo que no se borra es una ruta sin nombre (`_is_nameless_path`). El
    `rmtree` es best-effort e `ignore_errors=True`, así que sobre la raíz no
    habría excepción que contar: se avisa y se vuelve.
    """
    try:
        if staged.is_dir() and not staged.is_symlink():
            if _is_nameless_path(staged):
                LOG.warning(
                    "el stageado %s no nombra un fichero: no se borra nada", staged
                )
                return
            shutil.rmtree(staged, ignore_errors=True)
            if staged.exists():
                LOG.warning("el stageado %s sigue en staging tras el borrado", staged)
            return
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo inspeccionar %s: %s", staged, exc)
        return
    _unlink_quietly(staged)


def _partial_pid(name: str) -> int | None:
    """Pid codificado en `<dest>.partial-<pid>-<seq>`, si el nombre lo tiene.

    Se exige la forma **exacta** que crea `_move`: marca, pid y secuencia
    estrictamente numéricos. Un nombre como `wan2.2_vae.partial-backup` o
    `wan2.2_vae.partial-1234` (sin secuencia) no es un temporal de este módulo
    y nunca debe poder borrarse (R24).
    """
    _, mark, tail = name.rpartition(PARTIAL_MARK)
    if not mark:
        return None
    pid, dash, seq = tail.partition("-")
    if not dash or not pid.isdigit() or not seq.isdigit():
        return None
    try:
        return int(pid)
    except ValueError:  # dígitos que `int` no acepta (superíndices, etc.)
        return None


def _pid_alive(pid: int) -> bool:
    """`True` si el proceso `pid` existe. No envía ninguna señal.

    `os.kill(pid, 0)` **no** es una sonda de vida en Windows: CPython acaba en
    `TerminateProcess` (podría matar al dueño real) y un PID inexistente lanza
    un `OSError` genérico que se leía como «vivo», así que los
    `*.partial-<pid>-<seq>` huérfanos no se barrían nunca y la caché de staging
    crecía sin límite. En Windows se pregunta al kernel con
    `OpenProcess`+`WaitForSingleObject(0)`, que solo espera.
    """
    if os.name == "nt":
        import ctypes

        synchronize = 0x0010_0000  # PROCESS_SYNCHRONIZE
        wait_timeout = 0x0000_0102
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(synchronize, False, int(pid))
        if not handle:
            # ERROR_INVALID_PARAMETER (87) = ese PID no existe. Acceso denegado
            # significa que existe pero es de otro usuario: está vivo.
            return kernel32.GetLastError() == 5
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _partial_is_in_flight(partial: Path) -> bool:
    """True si el temporal puede estar en uso por un proceso vivo.

    El `SIGKILL` de un proceso deja el temporal atrás para siempre, y ninguna
    retención lo borra (`cache_policy` solo mira los `dest_path` de los
    manifiestos y los `.hf_stage*`), así que hay que barrerlos. Pero un
    temporal reciente de un proceso **vivo** puede ser una copia en curso de
    otro `acquire` concurrente: borrarla haría fallar su `rename`. Solo se
    considera huérfano lo que lleva inactivo más de `STALE_PARTIAL_AGE_S`.

    D2: un temporal **sin** pid en el nombre —`muerto.bin.incomplete`, que
    deja el cliente cuando se interrumpe una descarga— no tiene proceso al que
    preguntar, y responder «en vuelo» para siempre lo hacía inbarrable: cada
    camino terminal se encontraba con él, avisaba de la descarga ajena y no
    borraba nada, así que la caché del cliente dejaba de limpiarse para siempre
    y el `<staged>.incomplete` sobrevivía a todas las pasadas. Se aplica la
    misma regla de edad que a los que sí lo tienen: reciente se respeta,
    inactivo más de `STALE_PARTIAL_AGE_S` es basura.
    """
    owner = _partial_pid(partial.name)
    if owner == os.getpid():
        return True
    try:
        age = time.time() - partial.stat().st_mtime
    except (OSError, ValueError):
        return True
    if owner is None:
        return age < STALE_PARTIAL_AGE_S
    if not _pid_alive(owner):
        return False
    # A live owner's copy can run longer than the stale threshold. mtime is
    # not a liveness signal: a slow or blocked cross-filesystem copy may leave
    # it unchanged while the process still owns the install temp.
    return True


def _drop_stale_partials(directory: Path, dest_name: str) -> None:
    """Borra los `<dest>.partial-<pid>-<seq>` huérfanos de `directory`.

    El barrido se restringe al destino que se está instalando **y** a la forma
    exacta que crea `_move`: `<dest_name>.partial-<pid>-<seq>`, con pid y
    secuencia numéricos. Barrer `*{PARTIAL_MARK}*` sobre todo el directorio
    borraba activos legítimos cuyo nombre contiene la marca por casualidad
    (`wan2.2_vae.partial-backup`, `wan2.2_vae.partial-1234`) y recorría además
    cada activo del árbol de modelos una vez por necesidad (R24).

    El destino es compartido, así que el pid del temporal identifica a su dueño.
    Un pid vivo siempre conserva su temporal, incluso si el mtime es antiguo:
    una copia cross-filesystem lenta puede superar `STALE_PARTIAL_AGE_S` sin
    terminar. El temporal de un proceso que ya no existe se puede limpiar; el
    destino nunca entra en el barrido.
    """
    prefix = f"{dest_name}{PARTIAL_MARK}"
    try:
        candidates = list(directory.glob(f"{glob.escape(prefix)}*"))
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo listar %s: %s", directory, exc)
        return
    for partial in candidates:
        if _partial_pid(partial.name) is None:
            continue
        if _partial_is_in_flight(partial):
            continue
        _unlink_quietly(partial)


def _move(src: Path, dest: Path) -> None:
    """Mueve `src` a `dest` de forma atómica, aunque estén en volúmenes distintos.

    `os.replace` falla con `EXDEV` si staging y destino están en filesystems
    diferentes, que es perfectamente posible (`custom_nodes/*` vive bajo
    `COMFY_APP_DIR`). En ese caso **no** se escribe directo a `dest`: una copia
    interrumpida dejaría un parcial que el «skip» daría por bueno (F7). Se copia
    a un temporal del propio directorio de destino y se renombra con
    `os.replace`, que sí es atómico dentro de un mismo filesystem.

    Si ese `rename` también falla (ENOSPC en overlayfs/btrfs al reemplazar,
    EPERM en FUSE/NFS/SMB) se **falla cerrado**: se borra el temporal y se
    propaga el error, que `_acquire_one` convierte en una entrada fallida. Antes
    aquí había un último recurso que copiaba directo a `dest`: dejaba el activo
    a medias bajo su nombre definitivo y el «skip» de la siguiente pasada lo
    aceptaba como completo. Entre un activo perdido y un activo corrupto que
    parece bueno, la política de integridad prefiere el primero: se reintenta.

    Las tres restricciones se reconcilian así: (1) el destino solo se publica
    por `rename`, nunca por una copia interrumpible (F7); (2) si el rename es
    imposible se falla en lugar de degradar a copia directa (R12); (3) el
    original staged no se destruye hasta que el rename ha tenido éxito, así que
    un fallo deja la descarga intacta para reintentar.

    R82: en el camino de mismo filesystem el contenido del stageado se fuerza a
    disco **antes** del `os.replace`. El rename átomico garantiza que el nombre
    aparezca de una pieza, no que los bytes que nombra estén en el disco: los
    escribió el cliente `hf`, y un corte de luz justo después podía publicar el
    nombre nuevo con contenido incompleto. Sincronizar solo el directorio no
    basta, que es justo lo que el docstring de `_fsync_dir` dice querer evitar.
    """
    _fsync_file(src)
    try:
        os.replace(src, dest)
    except OSError as exc:
        if getattr(exc, "errno", None) != errno.EXDEV:
            raise
    else:
        # Mismo filesystem: el rename ya es atómico y el contenido ya está en
        # disco; queda la entrada del directorio.
        _fsync_dir(dest.parent)
        return
    seq = next(_partial_seq)
    # D6: se construye sobre el padre, como `_sidecar`. `dest.with_name(...)`
    # lanza `ValueError: ... has an empty name` cuando el último componente no
    # es un nombre —`/`, `/.`, `/..`—, y esa excepción salía de `acquire()` sin
    # informe. Inalcanzable desde `acquire()` (la guarda de `_acquire_one` lo
    # rechaza antes), pero `_move` también la llaman directamente.
    tmp = _sidecar(dest, f"{PARTIAL_MARK}{os.getpid()}-{seq}")
    # R83: el destino es quien paga la copia cross-device. Comprobar que le
    # cabe antes de escribir evita agotar los intentos copiando GBs para
    # fallar en el `rename` final.
    try:
        needed = src.stat().st_size
    except (OSError, ValueError):
        needed = 0
    _destination_has_room(dest, needed)
    try:
        _copy_and_fsync(src, tmp)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    try:
        os.replace(tmp, dest)
    except OSError:
        _unlink_quietly(tmp)
        raise
    _fsync_dir(dest.parent)
    # R25: el stageado ya está instalado, así que borrarlo es lo esperado; si
    # no se puede (staging de solo lectura, EPERM en FUSE/NFS/SMB) queda una
    # copia completa —20 GB o más— en staging y el informe diría «downloaded»
    # sin mencionarlo. Ese coste en disco se ve en el log.
    _unlink_quietly(src, warn=True)


def _free_bytes(path: Path) -> int | None:
    """Espacio libre real del filesystem que contiene `path` (None si ilegible).

    `path` puede no existir todavía (staging y destino se crean al descargar),
    así que se sube por los ancestros hasta el primero que exista —resolviendo
    enlaces simbólicos— para medir el volumen correcto. Si ningún ancestro
    existe se devuelve None: medir `/` informaría del volumen equivocado y la
    guarda debe fallar cerrada en lugar de arriesgarse.
    """
    try:
        current = path
        while True:
            if current.exists():
                return shutil.disk_usage(current.resolve()).free
            parent = current.parent
            if parent == current:
                return None
            current = parent
    except (OSError, ValueError):
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


def _reap_token(token: Path) -> None:
    """Retira el subárbol de esta adquisición si la limpieza lo dejó vacío.

    D1: cada `acquire()` descarga bajo su propio `job-<pid>-<seq>`, así que un
    directorio vacío es basura **de este proceso** y borrarlo no puede tocar al
    vecino. Si quedó algo dentro —la `.cache` del cliente, que en el camino de
    éxito se conserva como siempre— el subárbol se queda entero: tirar parciales
    reanudables es decisión de la retención (`cache_policy`), no de la
    adquisición.
    """
    try:
        if not token.is_dir() or any(token.iterdir()):
            return
        token.rmdir()
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo retirar el subárbol vacío %s: %s", token, exc)


def _remove_partials(staged: Path, stage: Path) -> None:
    """Borra el fichero stageado y sus `.incomplete` tras cancelar o fallar.

    HuggingFace deja `<fichero>.incomplete` cuando se interrumpe una descarga;
    si sobrevive a la limpieza, una recuperación podría tomarlo por un activo
    completo. Con `--local-dir` el cliente actual los escribe bajo
    `.cache/huggingface/download/`, así que se barren las dos rutas: esa y la
    heredada, junto al fichero stageado.

    D1: `stage` es la raíz del subárbol de **esta** adquisición (el
    `--local-dir`), no el staging compartido. Barrer el compartido era
    precisamente lo que hacía daño: el barrido por nombre no distingue el
    `.incomplete` propio del de otra adquisición que esté bajando el mismo
    `remote_path`.

    Es *best-effort*: cada borrado va protegido y un fallo se anota a `debug`.
    `_drop_client_cache` es simétrica —tampoco propaga nunca—, pero avisa con
    `warning` porque dejar la caché significa que el informe promete una
    limpieza que no ha ocurrido.

    El patrón del barrido se **escapa** (`glob.escape`): el nombre del stageado
    viene del manifiesto, y un `remote_path` como `model[1].bin` construía un
    patrón con clase de caracteres que nunca casaba con el fichero literal, así
    que el `.incomplete` sobrevivía y una recuperación podía tomarlo por un
    activo completo (R40).
    """
    _remove_staged(staged)
    _unlink_quietly(_sidecar(staged, ".incomplete"))
    downloads = stage / ".cache" / "huggingface" / "download"
    try:
        leftovers = list(downloads.rglob(glob.escape(staged.name) + ".incomplete"))
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo listar %s: %s", downloads, exc)
        return
    for leftover in leftovers:
        _unlink_quietly(leftover)


def _is_staged_name(name: str, staged_name: str) -> bool:
    """True si `name` es el stageado o uno de sus ficheros auxiliares."""
    return name == staged_name or name.startswith(f"{staged_name}.")


def _in_flight_under(root: Path, exclude: Path | None) -> list[Path]:
    """Parciales bajo `root` que pueden pertenecer a otra adquisición viva.

    R93: cuando el staging era un árbol compartido, un `.incomplete` o un
    `.partial-*` que no era del stageado de esta adquisición podía ser la
    descarga en vuelo de otro `acquire`, y borrarlo entero se la cobraba. Se
    listan para poder decidir si el árbol de la caché se puede borrar completo.

    D1: desde que cada adquisición trabaja bajo su propio `job-<pid>-<seq>`,
    `root` es ese subárbol y todo lo que contiene es suyo. La función sigue
    siendo la red que decide si se puede borrar el árbol completo, pero ya no
    depende de acertar con el nombre para no comerse al vecino: lo ajeno vive
    en un hermano que aquí no se recorre.
    """
    exclude_name = exclude.name if exclude is not None else None
    found: list[Path] = []
    for pattern in ("*.incomplete", f"*{PARTIAL_MARK}*"):
        try:
            candidates = list(root.rglob(pattern))
        except (OSError, ValueError) as exc:
            LOG.debug("no se pudo listar %s buscando %s: %s", root, pattern, exc)
            continue
        for candidate in candidates:
            if exclude_name is not None and _is_staged_name(candidate.name, exclude_name):
                continue
            if _partial_is_in_flight(candidate):
                found.append(candidate)
    return found


def _cache_entries_for(cache: Path, staged: Path) -> list[Path]:
    """Entradas de la caché que pertenecen al stageado `staged`.

    El cliente nombra sus entradas con el nombre del fichero que descarga
    (`<nombre>.incomplete`, `<nombre>.metadata`), así que la pertenencia se
    decide por nombre. Un fallo listando se anota y se devuelve lo que se pudo
    leer: el borrado es best-effort y no puede hacer más.

    D1: la caché que se recorre es la del subárbol de esta adquisición, así que
    el nombre solo tiene que distinguir **dentro** de lo propio —el stageado de
    sus auxiliares— y no frente a otra adquisición. Eso es lo que hace correcta
    por construcción la comparación por nombre que antes era ambigua.
    """
    entries: list[Path] = []
    try:
        for entry in cache.rglob("*"):
            if _is_staged_name(entry.name, staged.name):
                entries.append(entry)
    except (OSError, ValueError) as exc:
        LOG.warning("no se pudo listar la caché %s: %s", cache, exc)
    return entries


def _drop_client_cache(root: Path, staged: Path | None = None) -> None:
    """Elimina la caché del cliente bajo `root` cuando no se va a retomar.

    Solo se llama en los caminos terminales (cancelación o fallo definitivo):
    en un reintento se conserva para que el cliente retome la descarga.

    R93: borrar el árbol de staging **entero** se cobraba la descarga en vuelo
    de otra adquisición: su `.incomplete` desaparecía y su `rename` fallaba sin
    que nadie supiera por qué, así que una cancelación propia destruía el
    trabajo de otro job. Cuando hay parciales ajenos en vuelo solo se borra lo
    de esta adquisición, y el log dice qué se conserva y por qué.

    D1: ese recorte ya no se sostiene sobre el nombre. `root` es el subárbol
    `job-<pid>-<seq>` de esta adquisición, así que lo ajeno es un hermano que
    esta función no recorre; `_in_flight_under` queda como red interna para el
    caso —directo, no alcanzable desde `acquire()`— de llamarla con un `root`
    que contenga trabajo ajeno.

    E1: la llaman **siete** caminos terminales de `_acquire_one` (rc 130,
    veredicto del stageado cancelado, veredicto definitivo, stageado perdido
    antes del `rename`, `rename` cancelado y `rename` agotado), y **todos** le
    pasan el stageado: el octavo sitio que la menciona es su propia definición.

    No propaga (igual que `_remove_partials`), pero **sí avisa** si el árbol
    sigue ahí: si no, el camino de cancelación devolvía `status='cancelled'` y
    un informe que afirmaba un staging limpio mientras `.cache` sobrevivía.
    """
    cache = root / ".cache"
    try:
        if not cache.exists():
            return
    except (OSError, ValueError):
        return
    if _in_flight_under(root, staged):
        # R93: otra adquisición está escribiendo bajo este staging. Se borra
        # solo lo propio; si no se puede atribuir, no se borra nada.
        if staged is None:
            LOG.warning(
                "la caché del cliente en %s se conserva: hay una descarga ajena "
                "en vuelo y no se puede atribuir la pertenencia",
                cache,
            )
            return
        mine = _cache_entries_for(cache, staged)
        if not mine:
            LOG.warning(
                "la caché del cliente en %s se conserva: hay una descarga ajena "
                "en vuelo y ninguna entrada es de %s",
                cache,
                staged.name,
            )
            return
        for entry in mine:
            _remove_staged(entry)
        LOG.warning(
            "la caché del cliente en %s se conserva salvo lo de %s: hay una "
            "descarga ajena en vuelo",
            cache,
            staged.name,
        )
        return
    try:
        shutil.rmtree(cache)
    except (OSError, ValueError) as exc:
        LOG.warning("no se pudo borrar la caché del cliente en %s: %s", cache, exc)
        return
    if cache.exists():
        LOG.warning("la caché del cliente sigue en %s tras el borrado", cache)


def _int_kwarg_problem(name: str, value: object, *, allow_none: bool) -> str | None:
    """Mensaje si `value` no es un entero utilizable; `None` si lo es.

    D5: los kwargs numéricos de `acquire()` (`timeout_s`, `margin_bytes`,
    `max_attempts`) se validan con la misma regla de finito que `_env_int`
    aplica a sus variables de entorno, porque el camino por kwarg era
    estrictamente más débil: `HF_FETCH_TIMEOUT=inf` degradaba al valor
    documentado, pero `timeout_s=float('inf')` llegaba a `time.monotonic() +
    inf` y `max_attempts=1.5` a `range()`, con `TypeError` fuera de todo
    `except (OSError, ValueError)` y el plan abortado sin informe.

    `allow_none` distingue los dos sentidos que tiene `None` en la firma: en
    `timeout_s` y `margin_bytes` significa «no me lo pases, usa el entorno», y
    en `max_attempts` no significa nada —su valor por defecto es
    `DEFAULT_MAX_ATTEMPTS`, no el entorno—, así que un `None` ahí es un error y
    no un «usa el de siempre».
    """
    if value is None:
        return None if allow_none else f"{name} no puede ser None (se rechaza)"
    if isinstance(value, bool) or not isinstance(value, int):
        return f"{name} no es un entero (se rechaza): {value!r}"
    return None


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
    """Descarga los activos que faltan. `needs` son `AssetNeed` del resolvedor.

    Los kwargs tipados se validan al entrar (D5): uno que no es del tipo que
    dice su firma produce una entrada de fallo por necesidad, no una excepción.
    `stage_dir` es la excepción que **amplía** la firma: un `str` o un
    `os.PathLike` se coerciona a `Path` —es lo que `_unsafe_path` ya acepta, y
    rechazarlo haría `stage_dir` más estricto que `dest_path`—; lo que no es
    ninguna de las dos cosas se rechaza como el resto. `progress` se valida
    igual: `None` o algo invocable, y cualquier otra cosa es una entrada de
    fallo por necesidad, porque el `TypeError` de la llamada no lo ve ningún
    `except (OSError, ValueError)` del módulo.
    """
    # D5: los kwargs tipados se validan **antes** de tocar nada, igual que los
    # campos de la necesidad (R97) e igual que `_env_int` valida las variables
    # de entorno equivalentes. Un `stage_dir=7` llegaba a `Path(7)`, un
    # `env=['A=1']` a `environment.get(...)`, un `should_cancel=7` a la llamada
    # del sondeo y un `timeout_s='3600'` al `deadline`: todos respondían
    # `TypeError`/`AttributeError`, que ningún `except (OSError, ValueError)`
    # del módulo ve, así que el plan abortaba sin informe y con las necesidades
    # siguientes sin intentar. Se responde con una entrada por necesidad, que
    # es lo que el llamador sabe leer.
    kwarg_problems: list[str] = []
    stage_problem = _unsafe_path("stage_dir", stage_dir)
    if stage_problem is not None:
        kwarg_problems.append(stage_problem)
    for name, value in (
        ("timeout_s", timeout_s),
        ("margin_bytes", margin_bytes),
        ("max_attempts", max_attempts),
    ):
        problem = _int_kwarg_problem(name, value, allow_none=name != "max_attempts")
        if problem is not None:
            kwarg_problems.append(problem)
    if env is not None and not isinstance(env, Mapping):
        kwarg_problems.append(
            f"env no es un mapa de variables (se rechaza): {type(env).__name__}"
        )
    cancel_ok = should_cancel is None or callable(should_cancel)
    if not cancel_ok:
        kwarg_problems.append(
            f"should_cancel no es invocable (se rechaza): {type(should_cancel).__name__}"
        )
    # R2: `progress` se invoca en `_acquire_one` (cuatro veces), y un valor que
    # no es invocable respondía `TypeError` ahí —fuera de todo `except (OSError,
    # ValueError)`—, con el plan abortado sin informe. Misma regla que
    # `should_cancel`, que ya se validaba: `None` o invocable.
    progress_ok = progress is None or callable(progress)
    if not progress_ok:
        kwarg_problems.append(
            f"progress no es invocable (se rechaza): {type(progress).__name__}"
        )
    if kwarg_problems:
        message = "; ".join(kwarg_problems)
        safe_env = env if isinstance(env, Mapping) else {}
        rejected = AcquisitionReport(
            hf_cli=_public_hf_cli(hf_cli or resolve_hf_cli(safe_env)),
            stage_dir=str(stage_dir or default_stage_dir(safe_env)),
        )
        for need in needs:
            entry = _failure_entry(
                need,
                dest_path=need.dest_path,
                error=message,
                should_cancel=should_cancel if cancel_ok else None,
            )
            rejected.entries.append(entry)
            if entry.status == "cancelled":
                rejected.cancelled = True
        return rejected

    environment = os.environ if env is None else env
    cli = hf_cli or resolve_hf_cli(env)
    # R2: `_unsafe_path` acepta `str` y `os.PathLike`, pero `stage` se usa como
    # `Path` (`stage / f"job-<pid>-<seq>"`), así que un `str` llegaba al
    # operando `/` y respondía `TypeError`. Se coerciona aquí, una vez, con lo
    # que `_unsafe_path` ya dejó pasar.
    stage = Path(stage_dir) if stage_dir else default_stage_dir(env)
    timeout = timeout_s or _env_int(
        environment, "HF_FETCH_TIMEOUT", DEFAULT_HF_FETCH_TIMEOUT_S
    )
    margin = margin_bytes or _env_int(
        environment, "HF_DISK_MARGIN_BYTES", DEFAULT_DISK_MARGIN_BYTES
    )
    guard_enabled = (environment.get("HF_DISK_GUARD") or "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )

    # R3: `hf_cli` se coerciona a `str` para el informe. El campo está tipado
    # `str | None`, pero `_unsafe_path` acepta `os.PathLike` además de `str`, así
    # que un `Path` como cliente era válido para descargar y no serializable en
    # el informe público.
    report = AcquisitionReport(hf_cli=_public_hf_cli(cli), stage_dir=str(stage))
    # R97: `hf_cli` viene de fuera (`HF_CLI` lo escribe un operario, y cualquier
    # llamador directo puede pasar lo que quiera). Un valor que no es una ruta
    # llegaba a `subprocess.Popen`, cuyo `TypeError` no cubre ningún
    # `except (OSError, ValueError)`: el plan abortaba sin informe.
    cli_problem = _unsafe_path("HF_CLI", cli) if cli else None
    if cli is None or cli_problem is not None:
        message = (
            "sin cliente de HuggingFace (hf/huggingface-cli) disponible"
            if cli is None
            else cli_problem
        )
        for need in needs:
            # R95: este camino construía el `AcquiredEntry` a mano, así que un
            # job cancelado se reportaba `failed` y entraba en `report.failed`
            # —que el agente lee como «la descarga no completó todos los
            # archivos del grupo»—. `_failure_entry` es el sitio que convierte
            # fallo en cancelación (R79), y por aquí también tiene que pasar.
            # E2: no es el único que **sondea** `should_cancel`; ver su
            # docstring.
            entry = _failure_entry(
                need,
                dest_path=need.dest_path,
                error=message,
                should_cancel=should_cancel,
            )
            report.entries.append(entry)
            if entry.status == "cancelled":
                report.cancelled = True
        return report

    for index, need in enumerate(needs):
        if should_cancel is not None and should_cancel():
            report.cancelled = True
            # Cancelación a mitad de camino: las necesidades que ya no se van a
            # intentar se reportan para que el llamador sepa qué falta. Se emiten
            # siempre, también si la cancelación cae antes de la primera
            # entrada: guardarlas tras «si ya hay entradas» hacía desaparecer
            # todas las necesidades del informe, y el agente recibía «revisa
            # `failed`» con la lista vacía y sin ninguna pista de la cancelación
            # (el hook de cancelación sondea el estado persistido del job, así
            # que un job cancelado justo al arrancar cancela en la 1a consulta).
            # R3: esta es la otra construcción a mano de `AcquiredEntry`, y por
            # ella también pasan `_public_dest_path`/`_public_filename`: salen
            # de la necesidad sin coercionar, y el informe público los publicaba
            # tal cual —una lista o un entero de manifiesto acababa en el JSON
            # que lee el agente—.
            for remaining in needs[index:]:
                report.entries.append(
                    AcquiredEntry(
                        filename=_public_filename(remaining.filename),
                        dest_path=_public_dest_path(remaining.dest_path),
                        status="skipped",
                        # R23: «skipped» sin más es ambiguo —también significa
                        # «ya está en disco»—, así que la necesidad no intentada
                        # lo dice por sí misma en `error`. El informe público
                        # sigue publicando `entries` con la misma forma.
                        error="no intentado: adquisicion cancelada",
                    )
                )
            break
        entry = _acquire_one(
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
        report.entries.append(entry)
        if entry.status == "cancelled":
            report.cancelled = True
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
    mensaje de error si no. Un activo sin hash se acepta **aquí**: la política
    dice «where available», y exigirlo bloquearía activos que HuggingFace solo
    sirve por tamaño. Quien llama completa esa red con el tamaño declarado
    (`_check_staged`, `_inspect_dest`), y es allí donde se falla cerrado cuando
    no hay ni hash ni tamaño (R91, `UNVERIFIABLE`): esta función no ve el
    tamaño, así que por sí sola no puede distinguir un activo completo de uno
    truncado.

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


def _env_int(environment: dict[str, str], name: str, default: int) -> int:
    """`name` como entero; el valor por defecto si no está o no es un número.

    `int(float(...))` sobre el entorno lanzaba `ValueError` con un
    `HF_FETCH_TIMEOUT=abc` o un `HF_DISK_MARGIN_BYTES=1e`, y eso salía de
    `acquire()` antes de tocar nada: sin informe y sin descargas (R78). Un
    ajuste ilegible se sustituye por el documentado, y se dice en el log.

    R85: un `inf`/`-inf`/`1e400` no es `ValueError`, es `OverflowError` —un
    `ArithmeticError` que `int()` lanza al no poder convertir un infinito—, y
    tampoco se veía: la adquisición abortaba igual, sin informe y con las
    necesidades siguientes sin intentar. Se exige finito antes de convertir, y
    el `OverflowError` se captura igual por si acaso.
    """
    raw = (environment.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(raw)
        return int(value)
    except (ValueError, OverflowError):
        LOG.warning("%s no es un número (%r): se usa el valor por defecto", name, raw)
        return default


def _declared_bytes(need: Any) -> int:
    """Bytes que declara el manifiesto; 0 si no declara ninguno utilizable.

    `int(need.bytes or 0)` a pelo lanzaba `ValueError`/`TypeError` con una
    entrada de manifiesto mal formada (`"bytes": "muchos"`), y eso salía de
    `acquire()` sin entrada de informe: el síntoma de R13/R22, sobre datos que
    escribe un operador (R78). Un tamaño ilegible es «no declarado», que es lo
    conservador: sin tamaño la única red es el hash.

    R85: `"bytes": Infinity` también es alcanzable desde el manifiesto, porque
    `json.load` acepta el literal no estándar `Infinity`, y `int(inf)` responde
    `OverflowError`, que no es ni `TypeError` ni `ValueError`. Mismo desenlace:
    abortar sin informe. Un tamaño no finito es ilegible, así que se trata como
    no declarado.
    """
    raw = getattr(need, "bytes", 0)
    try:
        value = raw or 0
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(raw)
        return int(value)
    except (TypeError, ValueError, OverflowError):
        LOG.warning(
            "bytes declarados no numéricos (%r) para %s: se tratan como no declarados",
            raw,
            getattr(need, "filename", "?"),
        )
        return 0


def _declared_digest(need: Any) -> tuple[str | None, str | None]:
    """Algoritmo y hash que el manifiesto declara para `need`, si declara alguno.

    El algoritmo no es decorativo: `sha256` es el oid del objeto LFS (SHA-256
    crudo de los bytes) y `sha1` el del blob de git (con la cabecera
    ``blob <tamaño>\\0``). Ver `_verify_integrity`.
    """
    sha256 = getattr(need, "sha256", None)
    if sha256:
        return "sha256", str(sha256).lower()
    sha1 = getattr(need, "sha1", None)
    if sha1:
        return "sha1", str(sha1).lower()
    return None, None


def _digest_of(path: Path, algorithm: str) -> str:
    """Hash del contenido de `path` con el algoritmo que declara el manifiesto."""
    if algorithm == "sha256":
        return _raw_digest(path, "sha256")
    return git_blob_sha1(path)


def _sidecar(dest: Path, suffix: str) -> Path:
    """Ruta del sidecar de `dest`: junto a él, con el sufijo que le toque.

    R92: `dest.with_name(dest.name + suffix)` lanza
    `ValueError: PosixPath(/) has an empty name` cuando el último componente de
    la ruta está vacío, y esa excepción salía de `acquire()` sin informe. Se
    construye sobre el padre, que en una ruta siempre existe; quien llama
    rechaza antes un destino sin nombre (`_acquire_one`), así que esto es la
    red que queda para el resto de los llamadores.
    """
    return dest.parent / (dest.name + suffix)


def _read_sidecar(path: Path) -> dict[str, Any] | None:
    """Contenido del sidecar, o `None` si no está o no se entiende.

    Un sidecar corrupto se ignora en lugar de propagar: es una optimización, y
    perderla solo cuesta un hash. `ValueError` entra aquí porque una ruta con
    NUL responde con eso y no con `OSError` (R78).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        LOG.debug("sidecar ilegible en %s: se ignora", path)
        return None
    return data if isinstance(data, dict) else None


def _write_sidecar(path: Path, payload: dict[str, Any]) -> None:
    """Escribe el sidecar; un fallo se anota a `debug` y no se propaga.

    Es una caché: no poder escribirla cuesta un hash en la próxima adquisición,
    nunca una adquisición entera.
    """
    try:
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo escribir el sidecar %s: %s", path, exc)


def _integrity_cache_matches(
    cache: dict[str, Any] | None, dest: Path, digest: str, algorithm: str, size: int
) -> bool:
    """True si el sidecar sigue describiendo el fichero que hay en `dest`.

    La clave es `(mtime, size)`: si cambia cualquiera de los dos, el contenido
    ya no es el que se comprobó y hay que hasear de verdad. Un cambio de
    contenido que conserve el tamaño **cambia el mtime**, así que la cache se
    invalida (R76). El hash esperado también se compara: si el manifiesto fija
    otra revisión, la cache de la anterior no puede servir.

    R88: esa invalidación tiene un límite conocido y hay que nombrarlo. Quien
    **restaura** el `mtime` a propósito —`cp -p`, `rsync -a`, el restore de una
    copia de seguridad— deja un contenido distinto con el mismo tamaño y el
    mismo `mtime_ns` que el verificado, y esta cache lo da por bueno. No es un
    agujero que se pueda cerrar sin hasear en cada adquisición, que es
    exactamente lo que la cache existe para no hacer: es una optimización sobre
    el contenido ya comprobado, no una segunda verificación. La garantía de
    integridad —«ningún parcial debe parecer completo»— la sigue dando el hash,
    que se calcula siempre que el tamaño o el mtime se mueven; lo que aquí se
    ahorra es repetirlo cuando ninguno de los dos cambió.
    """
    if not isinstance(cache, dict):
        return False
    if cache.get("algorithm") != algorithm or cache.get("digest") != digest:
        return False
    try:
        stat = dest.stat()
    except (OSError, ValueError):
        return False
    return cache.get("mtime_ns") == stat.st_mtime_ns and cache.get("size") == stat.st_size


def _record_integrity(dest: Path, digest: str, algorithm: str) -> None:
    """Fija en el sidecar que el contenido actual de `dest` produjo `digest`."""
    try:
        stat = dest.stat()
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo medir %s para fijar su integridad: %s", dest, exc)
        return
    _write_sidecar(
        _sidecar(dest, INTEGRITY_SUFFIX),
        {
            "algorithm": algorithm,
            "digest": digest,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
        },
    )


@dataclass(frozen=True, slots=True)
class _DestVerdict:
    """Veredicto sobre el destino: reutilizable, con qué tamaño, y por qué no.

    `reason` solo se rellena cuando no es reutilizable, y es para el log: el
    «skip» sin más no explica por qué un activo que está en disco se vuelve a
    bajar, que es justo lo que hay que poder auditar (R76).

    `measured` dice si el veredicto salió de **medir** el destino. Cuando es
    `False` la única prueba era la marca `<dest>.installed` (R77), y quien
    reutiliza sabe que no puede darla por buena para siempre: la marca se
    retira en cuanto el destino se deja medir (R86/R90).
    """

    reusable: bool
    size: int | None = None
    reason: str = ""
    measured: bool = False


def _destination_looks_present(dest: Path) -> bool:
    """True si el destino está (o no se puede negar que esté) en disco (R77).

    `Path.exists()` devuelve `False` cuando no hay nada y **propaga** cuando el
    disco falla, que es exactamente la distinción que hace falta: un marcador de
    «instalado» cuyo fichero ya no existe es basura de una instalación que
    alguien borró, y darlo por presente saltaría un activo que no está. Un
    directorio tampoco es el activo.

    R86: pero cuando el disco falla **no** se responde que sí está. Eso era
    fail-open, y convertía la marca `<dest>.installed` en la única prueba: un
    destino borrado cuyo `exists()`/`stat()` sigue dando EIO/ESTALE se reportaba
    `skipped` con `bytes` del manifiesto —un activo ausente presentado como
    presente, que es «ningún parcial debe parecer completo» del revés—. Cuando
    `stat()` no puede hablar del destino se pregunta al **directorio**: si la
    entrada sigue ahí, el nodo existe aunque no se deje medir (que es el caso
    FUSE/NFS/SMB para el que la marca existe); si el directorio tampoco
    responde, se falla cerrado y se vuelve a descargar.
    """
    try:
        if not dest.exists():
            return False
        return not dest.is_dir()
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo comprobar si sigue en disco %s: %s", dest, exc)
        return _parent_lists(dest)


def _parent_lists(dest: Path) -> bool:
    """True si el directorio de `dest` sigue teniendo su entrada (R86).

    Es la evidencia que queda cuando el destino no se deja medir: borrar el
    activo quita la entrada del directorio, aunque el `stat()` del nodo siga
    fallando. Un fallo listando se responde que no está —fail-closed—, porque
    sin entrada no hay activo que reutilizar y descargar es lo conservador.
    """
    try:
        return dest.name in os.listdir(dest.parent)
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo listar %s: %s", dest.parent, exc)
        return False


def _inspect_dest(dest: Path, need: Any) -> _DestVerdict:
    """Decide si el destino se puede reutilizar sin volver a descargar.

    R76: «ningún parcial debe parecer completo». Un destino no vacío ya no
    basta. Se comprueba, en orden:

      1. que se pueda medir —y si no, que este módulo lo haya instalado y
         verificado (sidecar `<dest>.installed`, R77);
      2. que su contenido siga siendo el declarado: con hash, el hash manda y
         el tamaño declarado no se compara (R84, igual que `_check_staged`);
         sin hash, que el tamaño cuadre con el del manifiesto;
      3. que su contenido produzca el hash declarado, cacheado por
         `(mtime, size)` para no hasear 20 GB en cada adquisición.

    Cualquier discrepancia devuelve `reusable=False`, y quien llama vuelve a
    descargar: un activo truncado o corrupto se repara en lugar de darse por
    bueno para siempre.

    R91: si el manifiesto no declara ni hash ni tamaño utilizable no hay nada
    con lo que comparar, así que el destino **no** se reutiliza: sería «ningún
    parcial debe parecer completo» del revés. R96: la marca de «instalado sin
    medir» solo autoriza la reutilización si registra el hash que el manifiesto
    declara ahora; una marca con `digest: null` de una revisión hashless no
    basta cuando el manifiesto ha pasado a fijar uno.
    """
    measured = _dest_size(dest)
    declared = _declared_bytes(need)
    algorithm, expected = _declared_digest(need)
    if measured is None:
        marker = _read_sidecar(_sidecar(dest, INSTALLED_SUFFIX))
        if (
            marker is not None
            and marker.get("installed") is True
            and _destination_looks_present(dest)
        ):
            # R96: la marca autorizaba la reutilización con
            # `recorded in (None, expected)`, así que una marca escrita bajo
            # una revisión del manifiesto **sin** hash seguía valiendo cuando
            # el manifiesto pasaba a fijar un sha256: el activo se reutilizaba
            # sin verificar nada. Con hash declarado, la marca tiene que
            # registrar ese hash.
            # R91: y sin hash declarado hace falta un tamaño que la respalde,
            # la misma regla que aplica `_check_staged` al stageado.
            # D3: ese tamaño tiene que ser el que el manifiesto declara
            # **ahora**, no el que la marca registró cuando se instaló otra
            # revisión. Sin hash, `bool(declared)` era toda la comprobación: una
            # revisión que solo cambiaba `bytes` seguía reutilizando el activo
            # anterior, y el manifiesto nunca volvía a mandar. La marca compara
            # su `bytes` con el declarado igual que `_check_staged` compara el
            # medido.
            recorded = marker.get("bytes")
            # R5: `isinstance(True, int)` es verdadero en Python, así que
            # `recorded > 0` también lo era: una marca editada a mano con
            # `{"bytes": true}` y un manifiesto que declara `bytes: 1` autorizaba
            # la reutilización por `True == 1` y publicaba `bytes: true` en el
            # informe. Un booleano no es un tamaño.
            usable_size = isinstance(recorded, int) and not isinstance(recorded, bool)
            authorised = (
                marker.get("digest") == expected
                if expected is not None
                else bool(declared) and usable_size and recorded == declared
            )
            if authorised:
                # D3: el `bytes` de la marca no se hace eco sin más. Es un
                # número que este módulo escribió, pero sobre un destino que no
                # se deja medir: si no es un entero positivo, el informe dice
                # `0` —desconocido— en lugar de publicar el de la marca, que
                # podría no describir el activo que hay en disco.
                size = recorded if usable_size and recorded > 0 else 0
                if not size:
                    LOG.debug(
                        "la marca de instalada de %s no registra un tamaño "
                        "utilizable (%r): se informa 0",
                        dest,
                        recorded,
                    )
                return _DestVerdict(True, size)
        return _DestVerdict(False, None, "el destino no se pudo medir ni verificar")
    if measured <= 0:
        return _DestVerdict(False, measured, "el destino está vacío", measured=True)

    # R84: con hash declarado el contenido ya está fijado, así que comparar el
    # tamaño sería redundante —y peor: un manifiesto cuyo `bytes` no cuadre con
    # el fichero que HF sirve (otra revisión, un redondeo de LFS) volvería a
    # bajar en **cada** adquisición un activo ya verificado, justo lo que la
    # reutilización sin redescarga prohíbe. `_check_staged` ya lo hacía así
    # («con hash el contenido ya esta fijado»); aquí la misma regla, porque si
    # no el destino correcto nunca llegaba a la caché de integridad de abajo.
    # El tamaño declarado solo guarda los activos publicados **sin** hash.
    if algorithm is None and declared and measured != declared:
        return _DestVerdict(
            False,
            measured,
            f"tamaño {measured} != {declared} declarado en el manifiesto",
            measured=True,
        )
    if algorithm is None:
        # R91: sin hash y sin tamaño declarado tampoco hay nada que comprobar
        # en el destino: se reutilizaría un activo del que no se puede afirmar
        # que esté completo. Se falla cerrado y se vuelve a bajar.
        if not declared:
            return _DestVerdict(False, measured, UNVERIFIABLE, measured=True)
        # Sin hash no hay nada más que comprobar: la política es «where
        # available», y exigirlo bloquearía activos que HuggingFace solo sirve
        # por tamaño.
        return _DestVerdict(True, measured, measured=True)

    cache = _read_sidecar(_sidecar(dest, INTEGRITY_SUFFIX))
    if _integrity_cache_matches(cache, dest, expected, algorithm, measured):
        return _DestVerdict(True, measured, measured=True)

    try:
        actual = _digest_of(dest, algorithm)
    except (OSError, ValueError) as exc:
        return _DestVerdict(
            False,
            None,
            f"no se pudo verificar el destino {dest}: {exc}",
            measured=True,
        )
    if actual != expected:
        return _DestVerdict(
            False,
            measured,
            f"{algorithm} esperado {expected}, obtenido {actual}",
            measured=True,
        )
    _record_integrity(dest, expected, algorithm)
    return _DestVerdict(True, measured, measured=True)


def _unsafe_path(field: str, value: object) -> str | None:
    """Mensaje si `value` no es una ruta utilizable; `None` si lo es.

    R97: un campo que acaba en una ruta (`dest_path`, y el `hf_cli` que se
    valida con esta misma función en `acquire`) tiene que ser una ruta. Quien
    llama a `acquire()` directamente puede pasar cualquier cosa, y un
    `dest_path` que es una lista o un entero hacía que `Path(...)` respondiera
    `TypeError` —fuera de todo `except (OSError, ValueError)`—, con el plan
    entero abortado y sin informe. Los caracteres de control se comprueban
    igual que en el resto de los campos.

    Residuo de R97: `os.PathLike` solo promete un `__fspath__`, y ese puede
    devolver `bytes`. `os.fspath` lo devuelve tal cual, pero `Path(...)`
    responde `TypeError: argument should be a str or an os.PathLike object
    where __fspath__ returns a str, not bytes` sobre él, con el mismo final.
    Se rechaza aquí, antes de construir nada.
    """
    if value is None:
        return None
    if not isinstance(value, (str, os.PathLike)):
        return f"{field} no es una ruta (se rechaza): {type(value).__name__}"
    if isinstance(value, os.PathLike):
        try:
            fspath: object = os.fspath(value)
        except TypeError:
            return f"{field} no es una ruta (se rechaza): {type(value).__name__}"
        if not isinstance(fspath, str):
            return (
                f"{field} no es una ruta (se rechaza): "
                f"{type(value).__name__} con __fspath__ en bytes"
            )
    return _unsafe_text(field, value)


def _unsafe_text(field: str, value: object) -> str | None:
    """Mensaje si `value` trae caracteres de control; `None` si es utilizable.

    Los datos del manifiesto los escribe un operador y este módulo los trata
    como no confiables. `pathlib` y `subprocess` responden a un NUL con
    `ValueError`, no con `OSError`, así que ni el `except OSError` de la
    descarga ni el del `rename` lo veían: un `dest_path` con NUL salía de
    `acquire()` **después** de haber pagado la descarga, sin entrada de informe
    y con las necesidades siguientes sin intentar (R78).
    """
    text = "" if value is None else str(value)
    for char in text:
        if ord(char) < 0x20 or ord(char) == 0x7F:
            return f"{field} con caracteres de control (se rechaza): {text!r}"
    return None


def _resolves_inside(path: Path, root: Path) -> bool:
    """True si `path` resuelve dentro de `root` (o es el propio `root`)."""
    try:
        resolved_path = path.resolve()
        resolved_root = root.resolve()
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo resolver %s respecto a %s: %s", path, root, exc)
        return False
    return resolved_path == resolved_root or resolved_root in resolved_path.parents


def _destination_has_room(dest: Path, size: int) -> None:
    """Falla con ENOSPC si el destino no puede absorber `size` bytes.

    R83: la guarda completa corre antes de descargar y no se reevalúa en un
    reintento de instalación a propósito (R36), pero el camino cross-device
    **consume** espacio en el destino —copia el activo entero a un temporal de
    su directorio— y ahí el ENOSPC se pagaba copiando GBs para fallar en el
    `rename` final, quemando los intentos y con ellos la descarga ya verificada.
    Comprobar solo el destino es un `statvfs`, y si el volumen no se deja medir
    no se inventa un fallo: lo dirá la copia, que es quien sabe.
    """
    free = _free_bytes(dest.parent)
    if free is not None and free < size:
        raise OSError(
            errno.ENOSPC,
            f"espacio insuficiente en el destino: {free} libres, "
            f"se necesitan {size}",
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


def _staged_size(staged: Path) -> int | None:
    """Tamaño del stageado, o `None` si no se puede medir.

    Se usa para dejar constancia del tamaño de lo que se instaló cuando el
    destino recién publicado no se deja medir: sin él, la marca de «instalado»
    (R77) solo podría decir «está», y el informe daría `bytes=0` de un activo de
    20 GB.
    """
    try:
        return staged.stat().st_size
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo medir el stageado %s: %s", staged, exc)
        return None


def _staged_shape(staged: Path) -> str:
    """Forma del stageado, para que un rc 0 sin fichero se explique en el informe.

    Se calcula **antes** de limpiar: la limpieza puede llevarse el directorio
    por delante y el mensaje perdería la única pista de qué pasó.
    """
    try:
        if staged.is_dir():
            return "dejó un directorio"
    except (OSError, ValueError):
        pass
    return "no hay fichero"


def _client_error(returncode: int, staged: Path, shape: str) -> str:
    """Mensaje de fallo del cliente, distinguiendo timeout y «no dejó nada».

    Un rc 0 que no deja el fichero no es un «fallo del cliente (rc=0)»: o el
    `remote_path` se materializó como directorio (entradas sin extensión del
    manifiesto, p.ej. `LICENSE`) o no se descargó nada, y el informe tiene que
    decirlo (R22).
    """
    if returncode == TIMEOUT_RETURNCODE:
        return "timeout"
    if returncode == 0:
        return f"el cliente no dejó el activo en {staged} ({shape})"
    return f"fallo del cliente (rc={returncode})"


def _staged_is_present(staged: Path) -> bool | None:
    """`True`/`False` si se pudo comprobar; `None` si el stageado no se pudo medir.

    `is_file()`/`stat()` hablan con el disco y pueden fallar (EIO/ESTALE
    hasheando un activo de varios GB en FUSE/NFS/SMB, o el fichero
    desapareciendo a mitad de la comprobación). Quien está a punto de
    renombrarlo trata `None` como «no está», que es lo conservador (R37).

    Pero `False` y `None` **no** son la misma noticia, y confundirlas falseaba
    el diagnóstico: un rc 0 con un stageado que no se pudo medir se reportaba
    como «el cliente no dejó el activo (no hay fichero)», con su `warning`
    correspondiente, cuando el fichero estaba ahí y lo que falló fue el disco
    (R71). El operador acababa repitiendo una descarga que iba a fallar igual.
    `None` es «no lo sé», y quien lo recibe tiene que decirlo en lugar de
    atribuírselo al cliente.
    """
    try:
        return staged.is_file() and staged.stat().st_size > 0
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo comprobar %s: %s", staged, exc)
        return None


@dataclass(frozen=True, slots=True)
class _StagedVerdict:
    """Veredicto de `_check_staged`: el motivo y si la descarga se repite.

    No todo fallo de verificación se reintenta. Un activo cuyo hash o cuyo
    tamaño no cuadra está corrupto o no es el que declara el manifiesto:
    bajarlo otra vez no arregla nada, así que el fallo es definitivo. Lo que sí
    se reintenta es una descarga que no dejó nada aprovechable —rc de fallo,
    timeout, rc 0 sin fichero— y, desde R37, una que no se pudo **comprobar**:
    un `OSError` al medir o hasear puede ser la carrera documentada (el stageado
    desapareciendo a mitad) o un EIO/ESTALE de FUSE/NFS/SMB, y en ninguno de los
    dos casos el fichero es la evidencia de que esté corrupto.
    """

    error: str
    retryable: bool


def _check_staged(staged: Path, need: Any, returncode: int) -> _StagedVerdict | None:
    """Valida el stageado tras la descarga: `None` si está listo para instalar.

    Devuelve el veredicto en cualquier otro caso, y **ninguno de esos motivos es
    una excepción**: medir y hasear el stageado toca el disco (`stat()`,
    `open()`), y un `OSError` ahí salía de `acquire()` sin entrada de informe
    —el síntoma de R13/R22— justo en la carrera que el módulo documenta: el
    stageado desapareciendo entre la comprobación y la lectura. Se trata como
    descarga fallida, que es lo que es: el fichero no se pudo verificar (R37).

    Cubre también lo que describía `_client_error`: rc 0 sin fichero, o con un
    `remote_path` que materializó como directorio.

    Y distingue, desde R71, «el cliente no dejó nada» de «no se pudo comprobar
    si dejó algo»: un stageado que no se mide no es prueba de que el cliente
    fallara, así que se reporta como inverificable —el motivo que ya usaba
    `_verify_integrity`— en lugar de como fallo del cliente.
    """
    try:
        if returncode != 0:
            shape = _staged_shape(staged)
            return _StagedVerdict(_client_error(returncode, staged, shape), True)
        present = _staged_is_present(staged)
        if present is None:
            return _StagedVerdict(
                f"no se pudo verificar el stageado {staged}: "
                "no se pudo comprobar si sigue en disco",
                True,
            )
        if not present:
            shape = _staged_shape(staged)
            LOG.warning(
                "el cliente terminó bien pero no dejó %s en staging (%s)",
                staged,
                shape,
            )
            return _StagedVerdict(_client_error(returncode, staged, shape), True)
        # Integridad «where available»: el manifiesto publica el hash que
        # HuggingFace declara (SHA-256 del objeto LFS o SHA-1 del blob git).
        verified = _verify_integrity(staged, need)
    except (OSError, ValueError) as exc:
        return _StagedVerdict(f"no se pudo verificar el stageado {staged}: {exc}", True)

    if verified is not True:
        return _StagedVerdict(f"integridad no verificada: {verified}", False)

    # Sin hash, el tamaño es la única red de seguridad: un activo vacío o
    # truncado no debe colarse. Con hash, el contenido ya está fijado por el
    # hash, así que comparar el tamaño sería redundante.
    expected = _declared_bytes(need)
    algorithm, _ = _declared_digest(need)
    if algorithm is None:
        # R91: sin hash **y** sin tamaño utilizable no queda ninguna red, y el
        # stageado se publicaba como `downloaded` sin haber comprobado nada:
        # un parcial truncado parecía completo, que es justo lo que el
        # invariante del módulo prohíbe. Se falla cerrado y sin reintento —no
        # es retryable: el problema es el manifiesto, no la descarga—.
        if not expected:
            return _StagedVerdict(UNVERIFIABLE, False)
        try:
            actual = staged.stat().st_size
        except (OSError, ValueError) as exc:
            return _StagedVerdict(f"no se pudo medir el stageado {staged}: {exc}", True)
        if actual != expected:
            return _StagedVerdict(
                f"integridad no verificada: tamano {actual} != {expected}", False
            )
    return None


def _dest_size(dest: Path) -> int | None:
    """Tamaño del destino si es un fichero regular; `None` si no se puede medir.

    Devuelve el `st_size` **tal cual**, incluido el `0` de un fichero vacío:
    medir es informar de lo que hay, y «medido y vacío» (`0`) no es la misma
    noticia que «no se pudo medir» (`None`). Quien llama decide qué hacer con
    cada una —el «skip» exige además `> 0`, así que un destino vacío se vuelve a
    descargar— y para eso tiene que poder distinguirlos (R74).

    `None` agrupa tres situaciones que para quien llama son la misma —«el activo
    no está verificado en disco»—: que no haya nada, que lo que haya no sea un
    fichero regular, y que no se haya podido **medir**. La tercera es la que
    importa: `Path.is_file()` y `Path.stat()` propagan cualquier `OSError` fuera
    de {ENOENT, ENOTDIR, EBADF, ELOOP}, y un EIO/ESTALE midiendo un destino en
    FUSE/NFS/SMB —el escenario que nombran los docstrings del módulo— salía de
    `acquire()` como excepción, sin entrada de informe y con las necesidades
    siguientes sin intentar (R69). La carrera entre el `is_file()` y el `stat()`
    de entonces tenía el mismo efecto: el fichero podía desaparecer entre las
    dos llamadas, y el segundo `stat()` —el del `bytes` del informe— volvía a
    hablar con el disco sin protección.

    Ante la duda se responde que no está, como `_staged_is_present`: bajar y
    verificar un activo que ya estaba cuesta ancho de banda, pero una excepción
    sin informe deja el modelo entero sin adquirir.

    R77: «no se pudo medir» ya no significa necesariamente «bájalo otra vez».
    Cuando el destino no se deja medir, `_inspect_dest` —quien llama a esta
    función— consulta además el sidecar `<dest>.installed`, que registra los
    destinos que este módulo instaló y verificó sin poder medirlos después. Un
    destino cuyo `stat()` falla siempre (EIO/ESTALE en FUSE/NFS/SMB) se
    reconoce así como presente en lugar de volver a descargarse en cada
    adquisición.
    """
    try:
        if not dest.is_file():
            return None
        return dest.stat().st_size
    except (OSError, ValueError) as exc:
        LOG.debug("no se pudo comprobar el destino %s: %s", dest, exc)
        return None


def _cancelled_now(should_cancel: Callable[[], bool] | None) -> bool:
    """Sondea la cancelación; `False` si no hay nadie a quien preguntar."""
    return should_cancel is not None and should_cancel()


def _public_text(value: object, what: str) -> str:
    """`value` tal y como se publica: siempre un `str` (R3).

    `AcquiredEntry.dest_path`/`.filename` están tipados `str` y
    `to_public_dict()` los publica sin transformarlos, pero vienen de la
    necesidad —dato de manifiesto— y de los kwargs de `acquire()`, que un
    llamador directo puede llenar con cualquier cosa. Un valor que no es una
    ruta se rechaza, pero **el rechazo se publicaba con el valor rechazado**:
    una lista o un entero acababa en el informe público y en el JSON que lee el
    agente.

    Se coerciona con lo que `_acquire_one` ya usa para construir su `Path`:
    `str` tal cual, `os.PathLike` por su texto. Lo que no es ninguna de las dos
    cosas no tiene texto que publicar, así que se dice qué era en su lugar.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, os.PathLike):
        try:
            return str(value)
        except (TypeError, ValueError):
            return f"<{what} no publicable: {type(value).__name__}>"
    return f"<{what} no publicable: {type(value).__name__}>"


def _public_dest_path(value: object) -> str:
    """`dest_path` coercido para el informe público (R3); ver `_public_text`.

    No es el único sitio por el que pasan esas entradas. Las construcciones a
    mano de `AcquiredEntry` son varias: `_failure_entry`, por donde entra **todo**
    fallo —el rechazo de kwargs de `acquire()`, el camino sin cliente y los
    rechazos de campos dentro de `_acquire_one`—; las entradas `skipped` de la
    cancelación en el bucle de `acquire()`; y las `skipped`/`cancelled`/
    `failed`/`downloaded` de `_acquire_one`, que se construyen sobre un `Path`
    ya coercionado con `str`. Todas las que parten de un valor de manifiesto o
    de un kwargs pasan por aquí o por `_public_filename`, que comparten la
    coerción de `_public_text`.

    R99: lo segundo era mentira hasta esta ronda. Las diez construcciones a mano
    de `_acquire_one` pasaban `filename=need.filename` sin coercer, así que un
    nombre que no era texto se publicaba crudo. Ahora `_acquire_one` calcula
    `safe_filename = _public_filename(need.filename)` una sola vez, antes de
    tocar el disco, y sus diez entradas —y las llamadas a `progress`, que
    también espera `str`— usan ese texto.
    """
    return _public_text(value, "dest_path")


def _public_filename(value: object) -> str:
    """`filename` coercido para el informe público (R3); ver `_public_text`.

    Misma regla y mismo motivo que `_public_dest_path`: el nombre del activo
    también es dato de manifiesto, se publica crudo en `entries` y en
    `to_public_dict()["failed"]`, y un valor que no es texto hacía fallar
    `json.dumps` del informe entero.

    R99: «hacía» es ya pasado. Las construcciones a mano de `AcquiredEntry`
    dentro de `_acquire_one` no entraban por aquí —ni por `_failure_entry`—, así
    que el nombre seguía llegando sin coercer al informe. `_acquire_one` pasa
    ahora todas las suyas por esta función, y el informe público nunca publica
    un `filename` que no sea `str`.
    """
    return _public_text(value, "filename")


def _public_hf_cli(cli: object) -> str | None:
    """`hf_cli` tal y como se publica: `str` o `None` (R3).

    `AcquisitionReport.hf_cli` está tipado `str | None` y `to_public_dict()` lo
    publica sin transformarlo, pero `_unsafe_path` acepta `os.PathLike` además
    de `str` —es lo que permite que `HF_CLI` sea un `Path`—, así que un
    `PathLike` llegaba tal cual al informe y `json.dumps` del dict público
    respondía `TypeError`. Es la misma invariante que R3 sobre `dest_path`: el
    informe público se serializa.
    """
    if cli is None:
        return None
    return _public_text(cli, "hf_cli")


def _failure_entry(
    need: Any,
    *,
    dest_path: object,
    error: str,
    attempts: int = 0,
    should_cancel: Callable[[], bool] | None = None,
) -> AcquiredEntry:
    """Entrada de fallo, salvo que el job esté cancelado: entonces es cancelada.

    R79: el presupuesto de intentos y las comprobaciones inmediatas del
    manifiesto y de la guarda de disco devolvían `failed` sin consultar
    `should_cancel`. Un job cancelado acababa en `report.failed`, que el agente
    lee como «la descarga no completó todos los archivos del grupo» en lugar de
    como cancelación. Cancelar manda siempre, así que **todo camino que devuelve
    una entrada de fallo** pasa por aquí en lugar de consultarlo por su cuenta.

    E2: eso no es «un solo sitio». `should_cancel` se consulta además donde la
    cancelación tiene que interrumpir algo **antes** de devolver nada: el sondeo
    de la descarga (`_run_hf_download`), el bucle de necesidades de `acquire`, y
    los cuatro caminos que construyen la entrada `cancelled` a mano porque
    cancelar no es un fallo y no lleva `error` (rc 130, el veredicto del
    stageado, el stageado perdido antes del `rename` y el fallo del `rename`).
    Lo que este sitio centraliza es la **conversión** fallo↔cancelación, no el
    sondeo.

    E3: simétricamente, hay **tres** construcciones manuales de
    `status='failed'` —el veredicto definitivo del stageado, el stageado perdido
    antes del `rename` y el `rename` agotado—. Ninguna pasa por aquí porque las
    tres van **después** de haber preguntado ya por la cancelación en el mismo
    bloque: preguntar otra vez sería un sondeo más, y un sondeo más puede
    devolver `true` justo cuando el llamador ya ha decidido que no. El resto de
    los caminos de fallo sí entran por aquí.

    R3: `dest_path` se coerciona con `_public_dest_path` antes de construir la
    entrada, y `filename` con `_public_filename`: son dato de manifiesto, y sin
    coercionarlos un `dest_path` —o un nombre— que no es una ruta se publicaba
    tal cual en el informe. Es el sitio por el que pasan también los caminos que
    construyen entradas fuera de `_acquire_one`.
    """
    safe_dest_path = _public_dest_path(dest_path)
    if _cancelled_now(should_cancel):
        return AcquiredEntry(
            filename=_public_filename(need.filename),
            dest_path=safe_dest_path,
            status="cancelled",
            attempts=attempts,
        )
    return AcquiredEntry(
        filename=_public_filename(need.filename),
        dest_path=safe_dest_path,
        status="failed",
        attempts=attempts,
        error=error,
    )


def _acquire_one(
    need: Any,
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
    # R78: los datos del manifiesto no son de confianza, y un NUL o un carácter
    # de control en cualquiera de los campos que acaban en una ruta o en la
    # línea de comandos hace que `pathlib`/`subprocess` respondan `ValueError`,
    # no `OSError`. Salía de `acquire()` sin entrada —y en el `rename`, después
    # de haber pagado la descarga—. Se rechazan aquí, antes de tocar el disco.
    # R97: `dest_path` además tiene que ser una ruta: `Path()` sobre una lista o
    # un entero responde `TypeError`, que ningún `except (OSError, ValueError)`
    # del módulo ve. La validación va antes de construir el `Path`.
    for name, value in (
        ("dest_path", need.dest_path),
        ("remote_path", need.remote_path),
        ("repo_id", need.repo_id),
        ("revision", getattr(need, "revision", None)),
        # R89: `repo_type` acaba en la línea de comandos igual que `repo_id`
        # (`--repo-type`), así que validarlo aquí es lo que hace el límite
        # uniforme. Sin él, un `repo_type` con caracteres de control solo lo
        # paraba el `ValueError` que `Popen` lanza por el NUL: un detalle de
        # implementación, no una guarda.
        ("repo_type", need.repo_type),
    ):
        problem = _unsafe_path(name, value) if name in _PATH_FIELDS else _unsafe_text(name, value)
        if problem is not None:
            return _failure_entry(
                need,
                dest_path=need.dest_path,
                error=problem,
                should_cancel=should_cancel,
            )

    # R99: `filename` es dato de manifiesto igual que `dest_path`, pero las diez
    # construcciones a mano de `AcquiredEntry` de esta función lo publicaban tal
    # cual —`_failure_entry` sí lo coerciona, y esas no entran por él—. Un
    # `filename` que no es texto acababa en `to_public_dict()`, en
    # `to_public_dict()["failed"]` y en el `json.dumps` del informe entero, que
    # respondía `TypeError`. Se coerciona una vez aquí y todas las salidas de
    # esta función —las entradas de informe y las llamadas a `progress`, que
    # también está tipado con `str`— publican ese texto.
    safe_filename = _public_filename(need.filename)

    dest = Path(need.dest_path) if need.dest_path else None
    # R92: un destino cuyo último componente no es un nombre de fichero (`/`,
    # `/.`, `//`, y también `/..`, que `Path` conserva como nombre) no es un
    # fichero. `Path('/')` se construye sin problemas, pero el primer sidecar
    # que se pide sobre él reventaba con
    # `ValueError: PosixPath(/) has an empty name`, y la excepción salía de
    # `acquire()` sin informe. Es alcanzable desde un manifiesto de operario,
    # porque `model_assets.resolve_dest` respeta una ruta absoluta tal cual.
    # D6: `/..` entraba por el agujero de `not dest.name` —`Path('/..').name`
    # es `'..'`, que no está vacío— y llegaba a `_drop_stale_partials(Path('/'),
    # '..')` y a `_move`, que barren y renombran bajo la raíz.
    if dest is not None and dest.name in ("", ".", ".."):
        return _failure_entry(
            need,
            dest_path=str(dest),
            error=f"destino sin nombre de fichero (se rechaza): {dest}",
            should_cancel=should_cancel,
        )

    # R76/R77: el destino se reutiliza solo si sigue siendo el activo que
    # declara el manifiesto. `_dest_size` mide dentro de un `try` —un EIO/ESTALE
    # (FUSE/NFS/SMB) o el fichero desapareciendo a mitad no puede abortar el
    # plan sin informe (R69)— y `_inspect_dest` añade la comprobación de
    # tamaño, de hash cacheado por `(mtime, size)` y, para lo que no se deja
    # medir, la marca de «instalado y verificado» (R77).
    if dest is not None:
        dest_verdict = _inspect_dest(dest, need)
        if dest_verdict.reusable:
            # R90: la marca de «instalado sin medir» (R77) solo describe un
            # destino que **no** se deja medir. Si el veredicto salió de medir,
            # la marca es basura de una instalación anterior y se retira aquí,
            # no solo en el camino de instalación: dejarla viva convertía un
            # sidecar caduco en la única prueba de un activo que ya no está
            # cuando el disco empieza a fallar (R86).
            if dest_verdict.measured:
                _unlink_quietly(_sidecar(dest, INSTALLED_SUFFIX))
            if progress:
                progress(safe_filename, "skipped")
            return AcquiredEntry(
                filename=safe_filename,
                dest_path=str(dest),
                status="skipped",
                bytes=dest_verdict.size or 0,
            )
        LOG.info("el destino %s no se reutiliza: %s", dest, dest_verdict.reason)

    if not need.repo_id or not need.remote_path or dest is None:
        return _failure_entry(
            need,
            dest_path=need.dest_path,
            error="activo sin origen declarado en el manifiesto",
            should_cancel=should_cancel,
        )

    # `remote_path` construye `staged` y se borra en los caminos de fallo: se
    # valida igual que el destino para que un `..` no apunte fuera de staging.
    remote = str(need.remote_path)
    if (
        Path(remote).is_absolute()
        or remote.startswith("~")
        or ".." in Path(remote).parts
        # R80: `Path('.').parts` es vacío, así que `stage / '.'` **es** el
        # directorio de staging. Un fallo terminal borraría el árbol entero con
        # `_remove_staged` —la copia en vuelo de otro `acquire` concurrente
        # incluida—, y el stageado coincidiría con el destino.
        or not Path(remote).parts
    ):
        return _failure_entry(
            need,
            dest_path=str(dest),
            error=f"remote_path inseguro (se rechaza por seguridad): {remote}",
            should_cancel=should_cancel,
        )
    expected = _declared_bytes(need)
    # D1: cada adquisición trabaja bajo su propio subárbol de staging
    # (`job-<pid>-<seq>`), que es también el `--local-dir` del cliente. La
    # pertenencia de un parcial pasa a ser un **prefijo de ruta** en lugar de un
    # nombre: con `staged = stage / remote`, el nombre era todo lo que había, y
    # una descarga ajena del mismo `remote_path` se clasificaba como propia
    # —`_is_staged_name` no podía distinguirlas—, así que el camino terminal
    # borraba el `.incomplete` y la caché de otro job en vuelo (R93 tapaba solo
    # el caso de nombre distinto). Con el subárbol, lo ajeno es un hermano.
    token = stage / f"job-{os.getpid()}-{next(_partial_seq)}"
    staged = token / remote
    # Un destino relativo se resolvería contra el CWD y podría escribir dentro
    # del repositorio: solo se aceptan rutas absolutas.
    if not dest.is_absolute():
        return _failure_entry(
            need,
            dest_path=str(dest),
            error=f"destino no absoluto (se rechaza por seguridad): {dest}",
            should_cancel=should_cancel,
        )
    # R80: si el destino cae dentro del staging, el cliente escribe directo al
    # nombre definitivo y `_move` degenera en `os.replace(dest, dest)`: un
    # parcial quedaría publicado bajo el nombre final. Se comparan las rutas
    # resueltas, no las literales.
    if _resolves_inside(dest, stage):
        return _failure_entry(
            need,
            dest_path=str(dest),
            error=(
                f"destino dentro del directorio de staging "
                f"(se rechaza por seguridad): {dest}"
            ),
            should_cancel=should_cancel,
        )
    # Un `SIGKILL` entre la copia y el rename de una ejecución anterior deja
    # `<dest>.partial-*` en el directorio de destino, y ninguna retención lo
    # borra. Se barre aquí, antes de descargar nada.
    _drop_stale_partials(dest.parent, dest.name)
    backoff_index = 0
    # R21: la descarga y la instalación se reintentan por separado. Una vez
    # verificado el fichero stageado, un fallo de instalación no vuelve a
    # bajarlo: solo se reintenta el `rename`, que es lo que falló.
    staged_ready = False

    for attempt in range(1, max_attempts + 1):
        # R36: la guarda solo corre cuando toca descargar. Existe para fallar
        # cerrado **antes** de gastar ancho de banda y escribir GBs que no
        # caben; una vez verificada la descarga, lo que queda es un `rename` (o
        # una copia del mismo tamaño en el camino EXDEV) y reevaluarla aquí
        # secuestraba el reintento de instalación de R21: un disco que se llenó
        # *después* de bajar el activo convertía un EPERM transitorio en
        # «espacio insuficiente» —un error que ya no describía nada—, dejaba el
        # activo verificado huérfano en staging sin una entrada que lo dijera y,
        # como un `acquire` posterior no reutiliza el stageado, volvía a
        # descargar para tropezar con la misma guarda: fallo permanente
        # autoinfligido. Un ENOSPC real de la instalación lo reporta el camino
        # de abajo, que es el que sabe qué falló.
        if guard_enabled and not staged_ready:
            # Staging y destino: pueden estar en filesystems distintos.
            free = _free_bytes_for([stage, dest.parent])
            if free is None:
                return _failure_entry(
                    need,
                    dest_path=str(dest),
                    error="espacio libre ilegible (fail-closed)",
                    attempts=attempt,
                    should_cancel=should_cancel,
                )
            if free < expected + margin:
                # No hay nada que limpiar: la guarda corre antes de descargar,
                # así que el stageado que pueda haber en staging no es de esta
                # adquisición, y borrarlo rompería la copia en vuelo de otro
                # `acquire` concurrente (R24). Se deja estar y se dice que falta
                # sitio, que es el problema real.
                return _failure_entry(
                    need,
                    dest_path=str(dest),
                    error=(
                        f"espacio insuficiente: {free / GiB:.1f} GiB libres, "
                        f"se necesitan {(expected + margin) / GiB:.1f} GiB"
                    ),
                    attempts=attempt,
                    should_cancel=should_cancel,
                )

        if progress:
            progress(safe_filename, f"downloading:{attempt}")

        if not staged_ready:
            try:
                argv = [
                    cli,
                    "download",
                    str(need.repo_id),
                    remote,
                    "--repo-type",
                    str(need.repo_type or "model"),
                    "--local-dir",
                    str(token),
                ]
                # La revisión fijada del manifiesto se pasa al cliente: sin ella
                # la descarga resuelve contra `main`, que es mutable.
                revision = getattr(need, "revision", None)
                if revision:
                    argv += ["--revision", str(revision)]
                returncode = _run_hf_download(
                    argv,
                    timeout=timeout,
                    should_cancel=should_cancel,
                )
            except (OSError, ValueError) as exc:
                return _failure_entry(
                    need,
                    dest_path=str(dest),
                    error=f"no se pudo ejecutar {cli}: {exc}",
                    attempts=attempt,
                    should_cancel=should_cancel,
                )

            if returncode == CANCELLED_RETURNCODE:
                # Cancelar no es un fallo: no se reintenta y el staging queda
                # limpio (fichero, `.incomplete` y caché del cliente) para que
                # la recuperación no encuentre un parcial que dé por bueno.
                _remove_partials(staged, token)
                _drop_client_cache(token, staged)
                _reap_token(token)
                if progress:
                    progress(safe_filename, "cancelled")
                return AcquiredEntry(
                    filename=safe_filename,
                    dest_path=str(dest),
                    status="cancelled",
                    attempts=attempt,
                )

            # Presencia, hash y tamaño del stageado. `_check_staged` no
            # propaga: medir y hasear toca el disco y un `OSError` es una
            # descarga fallida, no una excepción sin informe (R37).
            verdict = _check_staged(staged, need, returncode)
            if verdict is not None:
                # El cliente no dejó el fichero, no se pudo comprobar, o su
                # hash o su tamaño no cuadran. Se borra lo que haya dejado
                # —fichero o directorio— sin propagar nunca. Un activo corrupto
                # o que no es el declarado no se vuelve a bajar: el fallo es
                # definitivo; una descarga fallida o ilegible sí se reintenta
                # si quedan intentos (R22).
                # R70/R73: cancelar se respeta también aquí, antes de gastar el
                # backoff de la descarga, y **antes** de mirar el presupuesto de
                # intentos. El sondeo de `_run_hf_download` ya pasó —la descarga
                # acabó, mal— y el de la instalación, más abajo, no cubre este
                # camino: un job cancelado se esperaba 2 s/5 s/11 s igualmente.
                # Consultarlo después del presupuesto bastaba para que un
                # cancelado en el **último** intento se reportara `failed` —con
                # `max_attempts=1`, siempre—, que el agente lee como «la descarga
                # no completó todos los archivos del grupo» en lugar de como
                # cancelación: el síntoma de R70, un intento más tarde. Los otros
                # dos caminos de cancelación (el rc 130 y el reintento del
                # `rename`) devuelven `cancelled` sin mirar el presupuesto; este
                # era el único que el presupuesto podía adelantar. La limpieza es
                # la misma que en el rc de cancelación: aquí ya no se retoma nada.
                if should_cancel is not None and should_cancel():
                    _remove_partials(staged, token)
                    _drop_client_cache(token, staged)
                    _reap_token(token)
                    if progress:
                        progress(safe_filename, "cancelled")
                    return AcquiredEntry(
                        filename=safe_filename,
                        dest_path=str(dest),
                        status="cancelled",
                        attempts=attempt,
                    )
                if attempt >= max_attempts or not verdict.retryable:
                    _remove_partials(staged, token)
                    _drop_client_cache(token, staged)
                    _reap_token(token)
                    return AcquiredEntry(
                        filename=safe_filename,
                        dest_path=str(dest),
                        status="failed",
                        attempts=attempt,
                        error=verdict.error,
                    )
                # Reintento: se conserva `.incomplete` y la caché para que el
                # cliente retoma la descarga en lugar de empezar de cero.
                _remove_staged(staged)
                delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
                backoff_index += 1
                time.sleep(delay)
                continue

            # Descarga completa y verificada: lo que queda es instalar, y eso
            # no requiere bajarla otra vez (R21).
            staged_ready = True

        # El stageado puede desaparecer entre pasadas (una retención que limpie
        # `.hf_stage*` a mitad de la adquisición): si ya no está, la descarga
        # sí hay que repetirla. Se sale con `continue`, no se sigue: renombrar
        # un fichero que no existe es un fallo garantizado, así que la pasada
        # siguiente reentra en la rama de descarga —y vuelve a pasar por la
        # guarda de espacio— en lugar de inventarse un `rename` sobre nada
        # (R35). Si no quedan intentos, eso ya es el fallo definitivo y se
        # dice cuál es en lugar de dejar caer el bucle.
        if staged_ready:
            # R71: `False` («no está») y `None` («no se pudo medir») llevan a
            # la misma decisión —no se renombra a ciegas— pero no al mismo
            # informe: decir «desapareció» cuando el disco fue el que falló
            # manda a buscar una retención que no tuvo nada que ver.
            presence = _staged_is_present(staged)
            if presence is not True:
                # R79 (residuo de R73): cancelar manda **antes** que el
                # presupuesto de intentos, también aquí. Este camino hace la
                # misma limpieza que los de cancelación —`_remove_partials` y
                # `_drop_client_cache`—, así que un job cancelado en la última
                # pasada acababa en `report.failed` con el motivo del stageado
                # perdido o inmedible. Los otros caminos ya consultaban la
                # cancelación antes de mirar el presupuesto; este era el cuarto
                # que no.
                if _cancelled_now(should_cancel):
                    _remove_partials(staged, token)
                    _drop_client_cache(token, staged)
                    _reap_token(token)
                    if progress:
                        progress(safe_filename, "cancelled")
                    return AcquiredEntry(
                        filename=safe_filename,
                        dest_path=str(dest),
                        status="cancelled",
                        attempts=attempt,
                    )
                if attempt >= max_attempts:
                    _remove_partials(staged, token)
                    _drop_client_cache(token, staged)
                    _reap_token(token)
                    return AcquiredEntry(
                        filename=safe_filename,
                        dest_path=str(dest),
                        status="failed",
                        attempts=attempt,
                        error=(
                            f"no se pudo comprobar el stageado {staged} "
                            "antes de instalarlo"
                            if presence is None
                            else f"el stageado {staged} desapareció antes de instalarlo"
                        ),
                    )
                staged_ready = False
                continue
            staged_bytes = _staged_size(staged)

        # R81: preparar el directorio del destino y renombrar son dos operaciones
        # con semánticas de fallo distintas. Con `exist_ok=True`, un `os.mkdir`
        # que falla lleva a `Path.mkdir` a medir el padre con `self.is_dir()`, así
        # que un EIO midiéndolo se re-lanzaba y se atribuía a la instalación: el
        # informe decía «no se pudo instalar» de un `rename` que nunca llegó a
        # intentarse, se quemaba el backoff y el fallo definitivo borraba la
        # descarga verificada. Aquí se prepara el padre y, si no se puede, se
        # dice que es el directorio —y el stageado se conserva.
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except (OSError, ValueError) as exc:
            LOG.warning(
                "no se pudo preparar %s para instalar %s: %s", dest.parent, dest, exc
            )
            # R87: este camino construía el `AcquiredEntry` a mano, así que un
            # job cancelado se reportaba `failed` y entraba en `report.failed`:
            # el agente lo lee como «la descarga no completó todos los archivos
            # del grupo» en lugar de como cancelación. `_failure_entry` es el
            # sitio que convierte fallo en cancelación (R79), y por aquí también
            # tiene que pasar. E2: no es el único que **sondea** `should_cancel`;
            # ver su docstring.
            return _failure_entry(
                need,
                dest_path=str(dest),
                error=(
                    f"no se pudo preparar el directorio del destino "
                    f"{dest.parent}: {exc}"
                ),
                attempts=attempt,
                should_cancel=should_cancel,
            )

        try:
            _move(staged, dest)
        except (OSError, ValueError) as exc:
            # R21: un fallo de instalación es de la clase transitoria que
            # enumera `_move` (ENOSPC en overlayfs/btrfs al reemplazar, EPERM en
            # FUSE/NFS/SMB) y la descarga ya está verificada en staging. Como
            # `_move` no destruye el original hasta que el rename tiene éxito,
            # aquí se conserva el fichero **y** la caché del cliente y se
            # reintenta el rename en la pasada siguiente: tirarlos entregaba un
            # activo de varios GB ya pagado y empezaba de cero. La limpieza que
            # pedía F9 sigue vigente, un camino más abajo: solo cuando ya no
            # quedan intentos, que es el fallo definitivo.
            # R38: cancelar se respeta también aquí, y antes de gastar el
            # backoff. El único sondeo de `should_cancel` vivía en
            # `_run_hf_download`, que no se ejecuta cuando el stageado ya está
            # verificado: un job cancelado reintentando el rename se pasaba el
            # backoff esperando y acababa reportado como fallo, no como
            # cancelación.
            if should_cancel is not None and should_cancel():
                _remove_partials(staged, token)
                _drop_client_cache(token, staged)
                _reap_token(token)
                if progress:
                    progress(safe_filename, "cancelled")
                return AcquiredEntry(
                    filename=safe_filename,
                    dest_path=str(dest),
                    status="cancelled",
                    attempts=attempt,
                )
            if attempt >= max_attempts:
                _remove_partials(staged, token)
                _drop_client_cache(token, staged)
                _reap_token(token)
                return AcquiredEntry(
                    filename=safe_filename,
                    dest_path=str(dest),
                    status="failed",
                    attempts=attempt,
                    error=f"no se pudo instalar en {dest}: {exc}",
                )
            LOG.warning(
                "no se pudo instalar %s (intento %d/%d): %s; "
                "se reintenta el rename sin volver a descargar",
                dest,
                attempt,
                max_attempts,
                exc,
            )
            delay = BACKOFF_SECONDS[min(backoff_index, len(BACKOFF_SECONDS) - 1)]
            backoff_index += 1
            time.sleep(delay)
            continue

        # D1: el `rename` ya consumió el stageado, así que el subárbol de esta
        # adquisición se retira si quedó vacío. No se toca cuando todavía
        # contiene algo —la `.cache` del cliente se conserva en el camino de
        # éxito, igual que se conservaba bajo el staging compartido—.
        _reap_token(token)

        # R72: medir el destino recién instalado se hace **fuera** del `try` de
        # la instalación, y no puede reintentar nada. Mientras la medida vivía
        # dentro, un `OSError` midiendo el destino **después** de un `rename`
        # exitoso se atribuía a la instalación: el informe decía «no se pudo
        # instalar» de un activo que estaba instalado y correcto, se quemaba el
        # backoff y se volvía a descargar —contradiciendo el `warning` de al
        # lado, que promete «se reintenta el rename sin volver a descargar»—. Peor
        # aún en un destino cuyo `stat()` falla siempre (EIO/ESTALE en
        # FUSE/NFS/SMB): cada `acquire` lo medía mal, lo volvía a bajar y acababa
        # en `failed` con la caché del cliente borrada: un fallo permanente
        # autoinfligido sobre un activo que lleva GBs en disco. `_move` ya ha
        # publicado el destino, así que aquí solo queda **informar**: el activo
        # está, y lo único que no se sabe es su tamaño.
        try:
            installed = dest.stat().st_size
        except (OSError, ValueError) as exc:
            LOG.warning(
                "instalado %s, pero no se pudo medir su tamaño: %s", dest, exc
            )
            # R77: el activo está instalado y verificado, pero este destino no se
            # deja medir. Sin una marca, cada adquisición lo mediría mal, lo daría
            # por ausente y lo volvería a bajar —a ancho de banda completa, para
            # siempre—. La marca dice «este módulo lo instaló y lo verificó», y
            # lleva el tamaño del stageado para que el informe no tenga que
            # inventarse un `bytes=0`. Se invalida si el destino algún día se
            # deja medir y no cuadra, porque entonces mide `_inspect_dest`, no
            # esta marca.
            algorithm, expected = _declared_digest(need)
            _write_sidecar(
                _sidecar(dest, INSTALLED_SUFFIX),
                {
                    "installed": True,
                    "bytes": staged_bytes,
                    "algorithm": algorithm,
                    "digest": expected,
                },
            )
            if progress:
                progress(safe_filename, "downloaded")
            return AcquiredEntry(
                filename=safe_filename,
                dest_path=str(dest),
                status="downloaded",
                bytes=staged_bytes or 0,
                attempts=attempt,
                error=f"no se pudo medir el destino instalado {dest}: {exc}",
            )

        # R76: el destino ya está publicado y medido, así que la marca de
        # «instalado sin medir» (R77) ya no describe nada: se retira para que un
        # destino que vuelve a ser medible se evalúe por su tamaño y su hash.
        _unlink_quietly(_sidecar(dest, INSTALLED_SUFFIX))
        # Y se fija la integridad **sin volver a hasear**: el contenido que se
        # acaba de publicar es el que `_check_staged` verificó contra el hash del
        # manifiesto, así que el sidecar puede registrar ese `(mtime, size)` como
        # comprobado. Es lo que hace que la próxima adquisición salte el destino
        # sin leer 20 GB.
        algorithm, expected = _declared_digest(need)
        if algorithm is not None and expected is not None:
            _record_integrity(dest, expected, algorithm)

        if progress:
            progress(safe_filename, "downloaded")
        return AcquiredEntry(
            filename=safe_filename,
            dest_path=str(dest),
            status="downloaded",
            bytes=installed,
            attempts=attempt,
        )

    # El bucle siempre retorna salvo con `max_attempts <= 0`.
    # R87: ni este caso se salta la cancelación. Construir la entrada a mano
    # dejaba un job cancelado en `report.failed` con `cancelled=False`, que es
    # el residuo de R79 que rompe «cancelar manda siempre ... un solo sitio».
    return _failure_entry(
        need,
        dest_path=str(dest),
        error="sin intentos disponibles (max_attempts<=0)",
        attempts=max_attempts,
        should_cancel=should_cancel,
    )

