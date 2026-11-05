"""Regresión de `asset_acquire`: instalación, limpieza y estados del informe.

Estas pruebas viven en su propio fichero, y no dentro del módulo de producción
ni entre las del resolvedor de capacidades, porque `pyproject.toml` fija
`testpaths = ["tests"]` y pytest solo recoge `test_*.py`: dentro de
`asset_acquire.py` no se ejecutaban en el gate documentado
(`pytest studio/tests -q`) y un refactor podía reintroducir cualquiera de estos
defectos con la puerta verde.

Cubren los hallazgos R21–R25 de la revisión adversarial sobre la adquisición de
activos: qué se conserva cuando un `rename` falla, cómo se limpia el staging sin
lanzar excepciones, cómo se distinguen las necesidades no intentadas y qué
temporales se pueden barrer. Los de R35–R40 cubren la segunda ronda: que el
stageado perdido se vuelva a descargar en lugar de renombrarse en vacío, que la
guarda de disco no secuestre el reintento de instalación, que verificar el
stageado no pueda lanzar una excepción sin informe, que la cancelación se
respete también reintentando la instalación, que dos instalaciones concurrentes
no compartan temporal y que el barrido de `.incomplete` escape el nombre.
Dos de ellos fijan además la **forma** de la solución, no su resultado: la
secuencia del temporal se pide con un único `next()` atómico (con un contador de
verdad la carrera no es observable: 9600 temporales concurrentes, cero
colisiones) y el EIO al medir el stageado se comprueba en la llamada que precede
al `rename`, que es la que se lleva el disco por delante.

La ronda R69–R71 cubre lo que quedaba fuera: medir el **destino** también toca
disco, y un EIO o la carrera entre el `is_file()` y el `stat()` no pueden
abortar el plan sin informe; el backoff de la descarga se gasta solo después de
consultar `should_cancel`, igual que el de la instalación; y un stageado que no
se pudo medir se reporta como inverificable, no como «el cliente no dejó nada».
La ronda R72–R75 cierra lo que esas dejaron suelto: medir el destino **después**
de instalar no es instalar —un EIO ahí no puede atribuirse al `rename` ni volver
a bajar un activo que ya está en disco—; la cancelación se consulta antes de
mirar el presupuesto de intentos, no después, para que un cancelado en el último
intento no acabe en `report.failed`; `_dest_size` mide y no filtra, así que un
destino vacío es `0` y no «no se pudo medir»; y los dos mensajes de la
comprobación previa al `rename` —«no se pudo comprobar» y «desapareció»— quedan
fijados por separado.
"""

from __future__ import annotations

import errno
import hashlib
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from comfy_studio import asset_acquire
from comfy_studio.capabilities import AssetNeed

#: Contenido que escribe el `hf` de mentira. Su hash se fija en la necesidad
#: para que la verificación de integridad pase.
FAKE_CONTENT = b"pesos"

#: Raíz del árbol de fuentes: la ronda de cierre lanza
#: `scripts/storage_requirement.py` como subproceso, igual que
#: `test_capability_resolver.py`.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _acquire_need(
    *,
    filename: str = "model.bin",
    dest_path: str = "",
    bytes: int = 0,
    repo_id: str | None = "repo/model",
    remote_path: str | None = "model.bin",
    repo_type: str | None = "model",
    revision: str | None = None,
    sha256: str | None = None,
    sha1: str | None = None,
) -> AssetNeed:
    """Un `AssetNeed` completo: lo que consume `acquire`/`_acquire_one`."""
    return AssetNeed(
        filename=filename,
        dest_path=dest_path,
        bytes=bytes,
        asset_class="task",
        present=False,
        source="core",
        repo_id=repo_id,
        remote_path=remote_path,
        repo_type=repo_type,
        revision=revision,
        sha256=sha256,
        sha1=sha1,
    )


def _fake_download(content: bytes):
    """`_run_hf_download` de mentira: escribe `content` en el stage remoto."""

    def _run(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        target = Path(argv[argv.index("--local-dir") + 1]) / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return 0

    return _run


def _counting_download(content: bytes, counter: dict[str, int]):
    """`_fake_download` que además cuenta cuántas veces se bajó el activo."""

    def _run(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        counter["n"] = counter.get("n", 0) + 1
        return _fake_download(content)(argv, timeout=timeout, should_cancel=should_cancel)

    return _run


def _digest(content: bytes = FAKE_CONTENT) -> str:
    return hashlib.sha256(content).hexdigest()


def _staged(stage: Path, name: str) -> Path:
    """El fichero stageado bajo el subárbol `job-*` de la adquisición (D1).

    Cada `acquire()` descarga bajo su propio `stage/job-<pid>-<seq>`, así que la
    ruta completa no la sabe quien siembra o quien comprueba: se busca por
    nombre de fichero, que es el que fija el manifiesto.
    """
    found = sorted(stage.rglob(name))
    assert found, f"no hay ningún {name} bajo {stage}"
    return found[0]


def _under_stage(path: Path, stage: Path, name: str) -> bool:
    """True si `path` es el stageado `name` **de este staging** (D1).

    El nombre solo no basta: el destino de estas pruebas también se llama
    `out.bin`, y un `Path.stat`/`Path.is_file` parcheado por nombre se lo
    llevaría por delante yfalsearía el diagnóstico. Se exige además que el
    staging sea uno de sus ancestros, que es lo que identifica al stageado
    desde que cada adquisición tiene su propio subárbol.
    """
    return path.name == name and stage in path.parents


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Sustituye `time.sleep` por un registro: los backoff no se esperan.

    Los caminos de reintento duermen 2 s/5 s/11 s. Esperarlos no prueba nada
    (el retardo es política, no corrección) y encarece la suite; lo que sí
    importa es **cuándo** se duerme, así que el registro se devuelve para
    poder afirmar que un cancelado no se pasó el backoff.
    """
    slept: dict[str, float] = {}

    def _sleep(seconds: float) -> None:
        slept["total"] = slept.get("total", 0.0) + seconds

    monkeypatch.setattr(asset_acquire.time, "sleep", _sleep)
    return slept


# --- R21: un fallo de instalación transitorio no tira la descarga -------------


def test_a_transient_install_failure_is_retried_without_redownloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R21: el `rename` se reintenta; la descarga verificada no se vuelve a bajar.

    El invariante (3) de `_move` —el original staged no se destruye hasta que el
    rename ha tenido éxito— solo significa algo si el llamador lo respeta. El
    camino de fallo borraba el fichero y la caché del cliente, así que un
    ENOSPC puntual (overlayfs/btrfs) o un EPERM (FUSE/NFS/SMB) tiraba un activo
    de varios GB ya pagado y la siguiente adquisición empezaba de cero.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    cleanups: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    real_cleanup = asset_acquire._remove_partials

    def _count_cleanup(staged: Path, stage_dir: Path) -> None:
        cleanups["n"] = cleanups.get("n", 0) + 1
        real_cleanup(staged, stage_dir)

    monkeypatch.setattr(asset_acquire, "_remove_partials", _count_cleanup)
    real_drop_cache = asset_acquire._drop_client_cache

    def _count_drop_cache(stage_dir: Path, staged: Path | None = None) -> None:
        cleanups["cache"] = cleanups.get("cache", 0) + 1
        real_drop_cache(stage_dir, staged)

    monkeypatch.setattr(asset_acquire, "_drop_client_cache", _count_drop_cache)

    real_replace = os.replace
    calls = {"n": 0}

    def _replace(a, b):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            # Staging y destino en volúmenes distintos: se copia al temporal.
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        if calls["n"] == 2:
            # ENOSPC al reemplazar: la clase transitoria que enumera `_move`.
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", _replace)

    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 2, "el segundo intento es del rename, no de la descarga"
    assert downloads["n"] == 1, "la descarga verificada no se repite"
    assert cleanups.get("n", 0) == 0, "un fallo transitorio no puede tirar la descarga"
    assert cleanups.get("cache", 0) == 0, "la caché del cliente tampoco se tira"
    assert dest.read_bytes() == FAKE_CONTENT
    assert not list(stage.rglob("out.bin")), "el stageado se consume al instalar"
    assert not list(dest.parent.glob("out.bin.partial-*")), "no queda el temporal"


def test_an_exhausted_install_failure_still_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R21/F9: la limpieza se mantiene cuando el fallo ya es definitivo.

    Se reconcilia con F9 («un fallo al instalar se reporta, no aborta, y el
    staging queda limpio»): la limpieza no desaparece, se desplaza al agotarse
    los intentos. Mientras quede alguno, el stageado es la recuperación.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    def _boom(src, dest):  # noqa: ANN001
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(asset_acquire, "_move", _boom)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert entry.attempts == 2, "se agotan los intentos antes de darse por vencido"
    assert "no se pudo instalar en" in (entry.error or "")
    assert downloads["n"] == 1, "ni reintentando se vuelve a descargar"
    assert not list(stage.rglob("*")), "el staging queda limpio tras el fallo definitivo"
    assert not dest.exists()


def test_a_staged_file_that_vanishes_midway_is_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R21/R35: sin stageado no hay nada que reintentar: se vuelve a descargar.

    El stageado puede desaparecer entre pasadas (una retención que limpie
    `.hf_stage*` a mitad de la adquisición). El código lo detectaba y
    desactivaba `staged_ready`, pero **seguía en la misma pasada** hasta el
    `rename`, así que `_move` recibía un fichero que ya no existía: el activo se
    reportaba fallido sin haber vuelto a intentar la descarga, que es lo único
    que podría arreglarlo (R35).

    Aquí el stageado se borra dentro del `_move` —después de la verificación de
    la pasada 1, que es el momento real en que se pierde— y no dentro de la
    descarga, que era el caso que el test anterior cubría por accidente.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    real_move = asset_acquire._move
    moves: dict[str, int] = {}

    def _vanish_then_fail(src: Path, target: Path) -> None:
        moves["n"] = moves.get("n", 0) + 1
        if moves["n"] == 1:
            # La retención se lleva el stageado recién verificado, y el rename
            # de esta pasada falla por su cuenta (EPERM transitorio, R21).
            src.unlink()
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_move(src, target)

    monkeypatch.setattr(asset_acquire, "_move", _vanish_then_fail)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert downloads["n"] == 2, "el stageado perdido se vuelve a descargar"
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 3, "descarga, renuncia al rename y segunda descarga"
    assert moves["n"] == 2, "nunca se renombra un stageado que ya no existe"
    assert dest.read_bytes() == FAKE_CONTENT
    assert not list(stage.rglob("out.bin")), "el stageado se consume al instalar"


def test_a_staged_file_that_vanishes_with_no_attempts_left_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R35: el `continue` del reset no puede quedarse sin entrada que leer.

    Si el stageado desaparece en la última pasada ya no hay dónde reintentar, y
    el bucle no puede terminar sin más: caería en el «sin intentos
    disponibles» del final, que mentiría sobre `max_attempts`.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )
    _no_sleep(monkeypatch)

    def _vanish(src: Path, target: Path) -> None:
        src.unlink()
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(asset_acquire, "_move", _vanish)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "desapareció" in (entry.error or ""), entry.error
    assert entry.attempts == 2
    assert not list(stage.rglob("out.bin")), "el fallo definitivo limpia staging"
    assert not dest.exists()


# --- R36: la guarda de espacio no secuestra el reintento de instalación -------


def test_an_install_only_retry_is_not_preempted_by_the_disk_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R36: la guarda se evalúa antes de descargar, no en cada pasada.

    Los bytes esperados ya están en staging cuando lo que se reintenta es el
    `rename`. Reevaluar la guarda ahí —pidiendo `esperado + margen` otra vez—
    convertía un EPERM transitorio en «espacio insuficiente»: un error que ya no
    describía nada, con el activo verificado huérfano en staging y sin ninguna
    entrada del informe que lo mencionara. Como un `acquire` posterior no
    reutiliza el stageado, la siguiente pasada volvía a descargar para tropezar
    con la misma guarda: fallo permanente autoinfligido.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    # Sitio de sobra para la descarga; nada que al reintentar la instalación.
    guard_calls: dict[str, int] = {}

    def _free(paths: list[Path]) -> int:
        guard_calls["n"] = guard_calls.get("n", 0) + 1
        return 10000 if guard_calls["n"] == 1 else 100

    monkeypatch.setattr(asset_acquire, "_free_bytes_for", _free)

    real_move = asset_acquire._move
    moves: dict[str, int] = {}

    def _eperm_once(src: Path, target: Path) -> None:
        moves["n"] = moves.get("n", 0) + 1
        if moves["n"] == 1:
            raise OSError(errno.EPERM, "Operation not permitted")
        return real_move(src, target)

    monkeypatch.setattr(asset_acquire, "_move", _eperm_once)

    report = asset_acquire.acquire(
        [need],
        hf_cli="/fake/hf",
        stage_dir=stage,
        margin_bytes=2000,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 2, "el segundo intento es del rename, no de la descarga"
    assert guard_calls["n"] == 1, "la guarda no se reevalúa en un reintento de rename"
    assert downloads["n"] == 1, "la descarga verificada no se repite"
    assert "espacio" not in (entry.error or ""), "el error no puede ser de disco"
    assert dest.read_bytes() == FAKE_CONTENT
    assert not list(stage.rglob("out.bin")), "el stageado se consume al instalar"


def test_the_disk_guard_does_not_touch_a_staged_file_of_another_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R36/R24: la guarda falla antes de descargar, así que no hay nada suyo.

    El stageado que pueda haber en staging cuando la guarda corta no es de esta
    adquisición (un `acquire` no reutiliza el de otra pasada), y borrarlo
    rompería la copia en vuelo de otro `acquire` concurrente sobre el mismo
    destino. Se deja estar y se dice que falta sitio, que es el problema real.
    """
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    # Un stageado de una adquisición anterior: la retención no se lo llevó.
    leftover = stage / "out.bin"
    leftover.write_bytes(FAKE_CONTENT)
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, {})
    )

    report = asset_acquire.acquire(
        [need],
        hf_cli="/fake/hf",
        stage_dir=stage,
        margin_bytes=10**15,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "espacio insuficiente" in (entry.error or "")
    assert entry.attempts == 1
    assert leftover.is_file(), "el stageado ajeno no se borra por la guarda"
    assert not dest.exists()


# --- R37: un OSError verificando el stageado es una descarga fallida ----------


def test_an_unreadable_staged_file_is_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R37: hasear el stageado toca el disco, y eso puede fallar.

    `_verify_integrity` hace `stat()` y `open()` sobre el stageado. Un
    EIO/ESTALE hasheando un activo de varios GB en FUSE/NFS/SMB —o el stageado
    desapareciendo entre la comprobación y la lectura— salía de `acquire()` como
    excepción, sin entrada de informe: el síntoma de R13/R22, en la carrera que
    el módulo documenta. Es una descarga fallida, así que se reintenta.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    real_digest = asset_acquire._raw_digest
    reads: dict[str, int] = {}

    def _eio_once(path: Path, algorithm: str) -> str:
        reads["n"] = reads.get("n", 0) + 1
        if reads["n"] == 1:
            raise OSError(errno.EIO, "Input/output error")
        return real_digest(path, algorithm)

    monkeypatch.setattr(asset_acquire, "_raw_digest", _eio_once)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 2
    assert reads["n"] == 2, "el fallo de lectura se reintenta, no se propaga"
    assert downloads["n"] == 2, "sin stageado verificable se vuelve a descargar"
    assert dest.read_bytes() == FAKE_CONTENT


def test_an_io_error_measuring_the_stage_is_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R37/R61: la comprobación de presencia antes de instalar también toca disco.

    `_staged_is_present` se llama dos veces: al validar el stageado y otra vez
    justo antes del `rename`, cuando ya está verificado. Un EIO/ESTALE midiendo
    un activo de varios GB en FUSE/NFS/SMB en **ese** punto salía de `acquire()`
    como excepción, sin entrada de informe: el síntoma de R13/R22, en la carrera
    que el módulo documenta. Se responde que no está, que es lo conservador
    —quien pregunta está a punto de renombrarlo—, y el resultado es una descarga
    fallida que se reintenta, no un `rename` a ciegas.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    staged_name = "out.bin"
    real_stat = Path.stat
    # El fallo se arma **después** de verificar, es decir, en la comprobación que
    # precede al `rename`: si se armara antes, el test cubriría la validación
    # (que ya cubre el de al lado) y no esta. Y tiene presupuesto de uno, porque
    # un `stat` que falla siempre impediría también validar la segunda descarga.
    armed = {"on": False}
    budget = {"eio": 1}

    def _eio(self, *args, **kwargs):  # noqa: ANN001
        if armed["on"] and budget["eio"] and _under_stage(self, stage, staged_name):
            budget["eio"] -= 1
            armed["on"] = False
            raise OSError(errno.EIO, "Input/output error")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _eio)

    real_check = asset_acquire._check_staged

    def _arm_once_verified(staged_path: Path, need_: object, returncode: int):
        verdict = real_check(staged_path, need_, returncode)
        if verdict is None and budget["eio"]:
            armed["on"] = True
        return verdict

    monkeypatch.setattr(asset_acquire, "_check_staged", _arm_once_verified)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 2, "se reintenta la descarga, no se renombra a ciegas"
    assert downloads["n"] == 2, "un stageado que no se pudo medir se vuelve a bajar"
    assert dest.read_bytes() == FAKE_CONTENT
    assert not list(stage.rglob(staged_name)), "el stageado se consume al instalar"


def test_an_unverifiable_staged_file_reports_a_failed_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R37: sin intentos que queden, el fallo se informa y el staging se limpia."""
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    def _vanished(path: Path, algorithm: str) -> str:
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", str(path))

    monkeypatch.setattr(asset_acquire, "_raw_digest", _vanished)
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, {})
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", "hay informe, no una excepción"
    assert "no se pudo verificar" in (entry.error or ""), entry.error
    assert entry.attempts == 1
    assert not list(stage.rglob("*")), "el fallo definitivo limpia staging"
    assert not dest.exists()


def test_a_corrupt_staged_file_is_not_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R37: no todo fallo de verificación se reintenta.

    Una descarga que no dejó nada aprovechable, o que no se pudo comprobar, se
    reintenta. Un activo cuyo hash no cuadra está corrupto o no es el que
    declara el manifiesto: bajarlo otra vez gasta ancho de banda para llegar al
    mismo sitio, así que el fallo es definitivo y se dice en el primer intento.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(b"otros pesos"),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "integridad" in (entry.error or ""), entry.error
    assert entry.attempts == 1, "un activo corrupto no se vuelve a descargar"
    assert downloads["n"] == 1
    assert not dest.exists()
    assert not list(stage.rglob("out.bin")), "el stageado corrupto se limpia"


# --- R38: cancelar también se respeta reintentando la instalación -------------


def test_a_cancelled_job_does_not_burn_the_install_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R38: un cancelado durante el reintento del rename es un cancelado.

    El único sondeo de `should_cancel` vivía en `_run_hf_download`, que no se
    ejecuta cuando el stageado ya está verificado: el job se gastaba el backoff
    igualmente y acababa en el informe como **fallo**, no como cancelación.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )
    slept = _no_sleep(monkeypatch)

    def _boom(src: Path, target: Path) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(asset_acquire, "_move", _boom)

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1  # False al entrar, True al reintentar la instalación

    events: list[tuple[str, str]] = []
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
        should_cancel=_should_cancel,
        progress=lambda filename, state: events.append((filename, state)),
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.attempts == 1, "un cancelado no se reintenta"
    assert slept == {}, "ni se quema el backoff esperando"
    assert ("out.bin", "cancelled") in events, "el progreso lo dice también"
    assert not list(stage.rglob("out.bin")), "el staging queda limpio"
    assert not dest.exists()


# --- R22: la limpieza del camino de reintento es best-effort -----------------


def test_a_plain_failure_with_an_unremovable_staged_file_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R22: un rc de fallo con el staging sin permiso de escritura informa.

    El camino hacía `staged.unlink(missing_ok=True)` a pelo: con el stage
    bloqueado, el `PermissionError` salía de `acquire()` sin informe y las
    necesidades restantes no se intentaban jamás. Es el síntoma de R13, en un
    camino que ningún test conducía.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    def _download_then_fail(argv, *, timeout, should_cancel):
        target = Path(argv[argv.index("--local-dir") + 1]) / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        target.with_name(target.name + ".incomplete").write_bytes(b"parcial")
        # D1: el stageado vive en el subárbol de esta adquisición, así que es
        # ese directorio el que hay que dejar sin escritura.
        target.parent.chmod(0o500)  # ni el stageado ni su `.incomplete` se borran
        return 1

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    try:
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            max_attempts=1,
        )
    finally:
        for locked in stage.rglob("job-*"):
            locked.chmod(0o700)

    entry = report.entries[0]
    assert entry.status == "failed", "hay informe, no una excepción"
    assert "rc=1" in (entry.error or "")
    assert entry.attempts == 1
    assert not dest.exists()
    # El parcial sigue en disco porque no se pudo borrar; lo importante es que la
    # adquisición no abortó y el llamador tiene entrada que leer.
    assert _staged(stage, "out.bin").is_file()


def test_a_directory_shaped_staged_path_yields_a_report_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R22: un `remote_path` sin extensión puede materializarse como directorio.

    `config/models-studio.json` tiene entradas así (`LICENSE`,
    `qwen0.6bemo4-merge/Modelfile`). El `unlink` sobre el directorio lanzaba
    `IsADirectoryError` fuera de `acquire()`, también sin informe.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "Modelfile"
    need = _acquire_need(
        filename="Modelfile",
        dest_path=str(dest),
        bytes=10,
        remote_path="Modelfile",
        repo_id="repo/qwen",
    )

    def _download_a_directory(argv, *, timeout, should_cancel):
        target = Path(argv[argv.index("--local-dir") + 1]) / argv[3]
        target.mkdir(parents=True, exist_ok=True)
        (target / "pesos.bin").write_bytes(b"x" * 10)
        return 0

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_a_directory)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", "hay informe, no una excepción"
    assert "directorio" in (entry.error or ""), entry.error
    assert not dest.exists()
    assert not list(stage.rglob("Modelfile")), "el stageado se limpia"


# --- R23: «no intentado» no es lo mismo que «ya está en disco» ----------------


def test_not_attempted_needs_are_distinguishable_from_already_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R23: una necesidad descartada por la cancelación se explica a sí misma.

    `status='skipped'` significa «ya está en disco, no se descarga». Usarlo
    también para «no se intentó» hacía indistinguibles las dos cosas para
    quien lee las entradas una a una, así que la no intentada lo declara en
    `error` (el vocabulario de estados no cambia: `to_public_dict` sigue
    publicando las mismas claves).
    """
    stage = tmp_path / "stage"
    needs = [
        _acquire_need(
            filename=f"m{i}.bin",
            dest_path=str(tmp_path / f"m{i}.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path=f"m{i}.bin",
            sha256=_digest(),
        )
        for i in range(2)
    ]
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )

    cancel_calls = {"n": 0}

    def _should_cancel() -> bool:
        cancel_calls["n"] += 1
        return cancel_calls["n"] > 1  # False en la 1a pasada, True después

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=_should_cancel,
    )

    assert report.cancelled is True
    entries = {e.filename: e for e in report.entries}
    assert set(entries) == {"m0.bin", "m1.bin"}, "ninguna necesidad desaparece"
    assert entries["m0.bin"].status == "downloaded"
    assert entries["m1.bin"].status == "skipped"
    assert entries["m1.bin"].bytes == 0 and entries["m1.bin"].attempts == 0
    assert "no intentado" in (entries["m1.bin"].error or "")

    # La misma necesidad ya en disco sigue siendo «skipped», sin error y con
    # bytes: son dos situaciones distintas aunque compartan estado.
    on_disk = asset_acquire.acquire(
        [needs[0]],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    ).entries[0]
    assert on_disk.status == "skipped"
    assert on_disk.error is None, "«ya en disco» no es un fallo ni un pendiente"
    assert on_disk.bytes == len(FAKE_CONTENT)

    public = report.to_public_dict()
    assert set(public["entries"][0]) == {
        "filename",
        "dest_path",
        "status",
        "bytes",
        "attempts",
        "error",
    }
    assert public["failed"] == []
    assert public["cancelled"] is True


# --- R24: el barrido de temporales solo alcanza a los de este módulo ---------


def test_a_legit_asset_that_merely_looks_partial_survives_the_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R24: un activo legítimo cuyo nombre parece un temporal no se borra.

    El barrido recorría `*{PARTIAL_MARK}*` sobre **todo** el directorio de
    modelos y borraba cualquier cosa cuyo token final parseara como pid muerto:
    un `wan2.2_vae.safetensors.partial-backup` con datos reales desaparecía.
    Además escaneaba cada activo del árbol una vez por necesidad.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "wan2.2_vae.safetensors"
    dest.parent.mkdir(parents=True)

    # Activos legítimos: nombres que un barrido amplio confunde con temporales.
    backup = dest.parent / "wan2.2_vae.safetensors.partial-backup"
    backup.write_bytes(b"activo real" * 1000)
    no_seq = dest.parent / "wan2.2_vae.safetensors.partial-1234"
    no_seq.write_bytes(b"activo real" * 1000)
    foreign = dest.parent / "otro.safetensors.partial-999-1"
    foreign.write_bytes(b"activo real" * 1000)

    # Huérfano de verdad: la forma exacta, dejado por un proceso ya terminado.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    orphan = dest.parent / f"wan2.2_vae.safetensors.partial-{dead.pid}-7"
    orphan.write_bytes(b"0" * 4096)

    need = _acquire_need(
        filename="wan2.2_vae.safetensors",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="wan2.2_vae.safetensors",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )

    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    assert report.entries[0].status == "downloaded"
    assert backup.is_file(), "un activo legítimo no se borra por parecer parcial"
    assert no_seq.is_file(), "sin secuencia numérica no es un temporal de este módulo"
    assert foreign.is_file(), "el temporal de otro destino no entra en este barrido"
    assert not orphan.exists(), "el huérfano con la forma exacta sí se barre"


# --- D2: un `.incomplete` sin pid no puede bloquear la limpieza para siempre ---


def _seed_orphan(token: Path, name: str, *, age_s: float | None) -> Path:
    """Siembra un `.incomplete` huérfano bajo la caché del cliente (D2)."""
    downloads = token / ".cache" / "huggingface" / "download" / "repo" / "model"
    downloads.mkdir(parents=True, exist_ok=True)
    orphan = downloads / name
    orphan.write_bytes(b"parcial huerfano")
    if age_s is not None:
        when = time.time() - age_s
        os.utime(orphan, (when, when))
    return orphan


def test_an_orphaned_incomplete_without_a_pid_ages_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2: un huérfano sin pid deja de ser «en vuelo» cuando lleva inactivo
    más de `STALE_PARTIAL_AGE_S`.

    `_partial_is_in_flight` respondía `True` para **cualquier** nombre sin pid,
    que es la forma que deja el cliente cuando se interrumpe una descarga
    (`muerto.bin.incomplete`). Sin proceso al que preguntar, el temporal
    contaba como en vuelo para siempre: cada camino terminal se encontraba con
    él, avisaba de la descarga ajena y no borraba nada, así que la caché del
    cliente dejaba de limpiarse de forma permanente. Un temporal **reciente**
    sigue respetándose —puede ser la copia en curso de otro `acquire`—, así que
    la regla es la de siempre: la edad, no la ausencia de pid.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}
    stale = asset_acquire.STALE_PARTIAL_AGE_S + 60

    def _download_then_fail(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        token = Path(argv[argv.index("--local-dir") + 1])
        seen["token"] = token
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        _seed_orphan(token, "muerto.bin.incomplete", age_s=stale)
        (seen["token"] / ".cache" / "basura.dat").write_bytes(b"basura")
        return 1

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert report.entries[0].status == "failed", report.entries[0].error
    assert not seen["token"].exists(), (
        "el huérfano caducado ya no bloquea la limpieza: la caché se barre"
    )


def test_a_fresh_orphaned_incomplete_is_still_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D2 (regresión): reciente se respeta, aunque no tenga pid.

    El arreglo no puede quedarse en «sin pid, huérfano»: un `.incomplete` recién
    creado puede ser la descarga en curso de otra adquisición, y borrarla haría
    fallar su `rename`. Se conserva, y el log dice por qué.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download_then_fail(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        token = Path(argv[argv.index("--local-dir") + 1])
        seen["token"] = token
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        _seed_orphan(token, "muerto.bin.incomplete", age_s=None)
        return 1

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    _no_sleep(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            max_attempts=1,
        )

    assert report.entries[0].status == "failed", report.entries[0].error
    assert (seen["token"] / ".cache" / "huggingface" / "download").is_dir(), (
        "un huérfano reciente sin pid se respeta"
    )
    assert any("descarga ajena" in r.getMessage() for r in caplog.records), caplog.text


def test_a_pidless_partial_ages_out_only_past_the_stale_threshold(
    tmp_path: Path,
) -> None:
    """D2: la regla de edad es la misma con pid y sin pid.

    Se comprueba directamente sobre `_partial_is_in_flight`, que es donde vive
    la decisión: sin pid, reciente es «en vuelo» y caducado no; con pid de
    proceso vivo pasa lo mismo, y con pid muerto nunca está en vuelo.
    """
    young = tmp_path / "joven.bin.incomplete"
    young.write_bytes(b"parcial")
    old = tmp_path / "muerto.bin.incomplete"
    old.write_bytes(b"parcial")
    when = time.time() - asset_acquire.STALE_PARTIAL_AGE_S - 60
    os.utime(old, (when, when))
    # Un pid vivo: este proceso. Y uno muerto, de un hijo ya terminado.
    alive = tmp_path / "vivo.partial-1-1"
    alive.write_bytes(b"parcial")
    dead_pid = tmp_path / f"muerto.partial-{os.getpid()}-1"
    dead_pid.write_bytes(b"parcial")

    assert asset_acquire._partial_is_in_flight(young) is True, (
        "un huérfano sin pid reciente puede ser una copia en curso"
    )
    assert asset_acquire._partial_is_in_flight(old) is False, (
        "uno inactivo más de STALE_PARTIAL_AGE_S es basura"
    )
    assert asset_acquire._partial_is_in_flight(alive) is True
    # El pid propio siempre es «en vuelo»: es el temporal de esta adquisición.
    assert asset_acquire._partial_pid(dead_pid.name) == os.getpid()
    assert asset_acquire._partial_is_in_flight(dead_pid) is True


# --- R25: una copia staged que sobrevive a la instalación se ve ---------------


def test_a_staged_copy_that_survives_the_install_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R25: si el stageado no se puede borrar, el coste en disco se ve.

    Tras un EXDEV el stageado se borra; si ese borrado falla (staging de solo
    lectura, EPERM en FUSE/NFS/SMB) quedaba una copia completa —20 GB o más— en
    staging y el informe decía «downloaded» sin mencionarlo. Es el mismo patrón
    de silencio que R13.
    """
    src = tmp_path / "stage" / "out.bin"
    src.parent.mkdir(parents=True)
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    src.write_bytes(b"contenido" * 1000)

    real_replace = os.replace
    calls = {"n": 0}

    def _exdev_once(a, b):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    monkeypatch.setattr(os, "replace", _exdev_once)

    real_unlink = Path.unlink

    def _unlink_eperm(self, *args, **kwargs):  # noqa: ANN001
        if self == src:
            raise PermissionError(errno.EPERM, "Operation not permitted")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _unlink_eperm)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        asset_acquire._move(src, dest)

    assert dest.read_bytes() == b"contenido" * 1000
    assert src.is_file(), "el stageado sobrevivió: eso es lo que hay que decir"
    assert any("sigue en disco" in r.getMessage() for r in caplog.records), caplog.text


# --- R39: la secuencia del temporal es atómica entre hilos del pool -----------


def test_concurrent_installs_of_one_destination_never_share_a_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R39: dos adquisiciones concurrentes no publican el mismo temporal.

    `acquire` corre en un threadpool, así que dos adquisiciones del mismo
    destino pueden entrar a la vez en el camino cross-device de `_move`. El
    contador se incrementaba sin bloqueo (`contador += 1` lee, suma y escribe):
    dos hilos podían quedarse con el **mismo** nombre de temporal. Con fuentes
    idénticas el daño no se ve —por eso pasaba desapercibido—, pero con fuentes
    distintas se publica bajo el nombre definitivo, por dos `os.replace`, un
    fichero que nunca se reverifica: el hash se calculó sobre el stageado.
    """
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    sources = [stage / f"src{i}.bin" for i in range(2)]
    for index, source in enumerate(sources):
        source.write_bytes(bytes([65 + index]) * 4096)

    names: list[str] = []
    lock = threading.Lock()

    def _record_only(src: Path, dst: Path) -> None:
        # Sin copia real: lo que se observa es el nombre del temporal.
        with lock:
            names.append(dst.name)

    monkeypatch.setattr(asset_acquire, "_copy_and_fsync", _record_only)

    def _install(source: Path) -> None:
        try:
            asset_acquire._move(source, dest)
        except OSError:
            pass  # el rename final también es EXDEV: solo interesan los nombres

    def _cross_device_for(barrier: threading.Barrier, entered: set[int]):
        """`os.replace` que sincroniza los hilos antes de dejarles seguir."""

        def _cross_device(a, b):  # noqa: ANN001
            if threading.get_ident() not in entered:
                entered.add(threading.get_ident())
                barrier.wait(timeout=10)
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        return _cross_device

    # El punto de cruce se pone en el `os.replace` que abre `_move`, es decir
    # **antes** de tocar el contador, y el intervalo de cambio de hilo se baja
    # al mínimo para que la carrera sea real y no una quiniela.
    switch_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-9)
    try:
        for _ in range(100):
            barrier = threading.Barrier(len(sources))
            entered = set()
            monkeypatch.setattr(
                os, "replace", _cross_device_for(barrier, entered)
            )
            threads = [threading.Thread(target=_install, args=(s,)) for s in sources]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
    finally:
        sys.setswitchinterval(switch_interval)

    assert len(names) == 200, "cada instalación genera su temporal"
    assert len(set(names)) == len(names), f"temporal compartido: {sorted(names)}"
    for name in names:
        # La forma exacta que crea `_move` y que parsea el barrido de R24: si el
        # nombre cambia, los huérfanos de este módulo dejan de barrerse.
        assert name.startswith(f"out.bin{asset_acquire.PARTIAL_MARK}{os.getpid()}-")
        assert asset_acquire._partial_pid(name) == os.getpid()
    assert not list(dest.parent.glob("out.bin.partial-*")), "no queda temporal"


def test_the_partial_sequence_is_drawn_from_one_atomic_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R39/R63: la secuencia se pide con una sola operación atómica.

    El test de estrés de arriba no distingue una implementación de la otra, y no
    por flojera del test: con un contador de verdad la carrera **no es
    observable**. `contador += 1` a nivel de módulo son dos bytecodes y el GIL
    no cambia de hilo dentro de esa ventana —32 hilos y 300 rondas, intervalo de
    conmutación al mínimo, 9600 temporales: cero colisiones—. Así que el
    invariante hay que fijarlo por la **forma**, no por la probabilidad.

    Lo que se pincha aquí es que la secuencia sale de un `next()` sobre un
    iterador, y que el valor que ese `next()` devuelve es el que acaba en el
    nombre del temporal. El iterador se sustituye por uno que cruza los dos
    hilos **dentro** del `next()`: si la secuencia se calculara con un
    leer-sumarc-escribir sobre un entero de módulo, este envoltorio no se podría
    usar siquiera (`int` no tiene `__next__`) y la instalación reventaría antes
    de llegar al temporal, así que el test falla en lugar de pasar de largo.
    """
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    stage = tmp_path / "stage"
    stage.mkdir()
    sources = [stage / f"src{i}.bin" for i in range(2)]
    for index, source in enumerate(sources):
        source.write_bytes(bytes([65 + index]) * 4096)

    # La secuencia real es un `itertools.count()`: `next()` es atómico en CPython.
    assert isinstance(asset_acquire._partial_seq, itertools.count)

    names: list[str] = []
    handed_out: list[int] = []
    lock = threading.Lock()

    class _CrossingCount:
        """`itertools.count()` que sincroniza los hilos dentro del `next()`."""

        def __init__(self, parties: int) -> None:
            self._count = itertools.count()
            self._barrier = threading.Barrier(parties)

        def __next__(self) -> int:
            value = next(self._count)
            with lock:
                handed_out.append(value)
            # Las dos lecturas se cruzan aquí: ningún hilo puede terminar su
            # `next()` antes de que el otro haya pedido el suyo.
            self._barrier.wait(timeout=10)
            return value

    monkeypatch.setattr(asset_acquire, "_partial_seq", _CrossingCount(2))
    monkeypatch.setattr(asset_acquire, "_copy_and_fsync", lambda src, dst: names.append(dst.name))

    def _exdev(a, b):  # noqa: ANN001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", _exdev)

    failures: list[BaseException] = []

    def _install(source: Path) -> None:
        try:
            asset_acquire._move(source, dest)
        except OSError:
            pass  # el rename final también es EXDEV: solo interesan los nombres
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)

    threads = [threading.Thread(target=_install, args=(source,)) for source in sources]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == [], f"la instalación no debe romperse: {failures}"
    assert len(handed_out) == 2, "cada instalación pide su secuencia una vez"
    assert len(set(handed_out)) == 2, "las dos lecturas no comparten valor"
    assert len(names) == 2 and len(set(names)) == 2, f"temporal compartido: {names}"
    # El valor que `next()` entregó a cada hilo es el que se usó: si el nombre
    # llevara otra secuencia, el barrido de huérfanos de R24 perdería el rastro.
    used = {
        name.rpartition(asset_acquire.PARTIAL_MARK)[2].partition("-")[2] for name in names
    }
    assert used == {str(value) for value in handed_out}, (used, handed_out)
    for name in names:
        assert name.startswith(f"out.bin{asset_acquire.PARTIAL_MARK}{os.getpid()}-")
        assert asset_acquire._partial_pid(name) == os.getpid()
    assert not list(dest.parent.glob("out.bin.partial-*")), "no queda temporal"


# --- R40: el barrido de `.incomplete` escapa el nombre del stageado -----------


def test_a_leftover_incomplete_with_metacharacters_is_swept(
    tmp_path: Path,
) -> None:
    """R40: el patrón del barrido se escapa, no se construye a pelo.

    `rglob(staged.name + ".incomplete")` fabricaba el patrón con un nombre que
    viene del manifiesto: un `remote_path` como `model[1].bin` se interpretaba
    como clase de caracteres, así que el `.incomplete` literal **nunca** se
    borraba y una recuperación podía tomarlo por un activo completo. Peor aún,
    el patrón sin escapar casaba con el `.incomplete` de otro activo.
    """
    stage = tmp_path / "stage"
    downloads = stage / ".cache" / "huggingface" / "download" / "repo"
    downloads.mkdir(parents=True)
    staged = stage / "model[1].bin"
    staged.write_bytes(b"parcial")
    sibling = stage / "model[1].bin.incomplete"
    sibling.write_bytes(b"parcial")
    leftover = downloads / "model[1].bin.incomplete"
    leftover.write_bytes(b"parcial")
    # El `.incomplete` de otro activo: lo que el patrón sin escapar borraría.
    foreign = downloads / "model1.bin.incomplete"
    foreign.write_bytes(b"parcial")

    asset_acquire._remove_partials(staged, stage)

    assert not staged.exists(), "el stageado se borra"
    assert not sibling.exists(), "el `.incomplete` junto al stageado se borra"
    assert not leftover.exists(), "el de la caché del cliente también (R40)"
    assert foreign.is_file(), "el de otro activo no es de este barrido"
    assert [p.name for p in downloads.rglob("*")] == ["model1.bin.incomplete"], (
        "solo queda lo que no es de este activo"
    )


# --- R69: medir el destino toca disco, y eso no puede abortar el plan ---------


def _break_dest_stat_once(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> dict[str, int]:
    """El primer `stat()` del destino falla con `error`; el resto va al disco.

    Con presupuesto de uno a propósito: un destino que nunca se pudiera medir
    impediría también leer el `bytes` del fichero recién instalado, y la prueba
    acabaría cubriendo el camino de fallo de instalación en lugar del de R69.
    """
    real_stat = Path.stat
    calls = {"n": 0}

    def _stat(self, *args, **kwargs):  # noqa: ANN001
        if self == dest and calls["n"] == 0:
            calls["n"] += 1
            raise error
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)
    return calls


def _race_the_destination_away(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> dict[str, int]:
    """`is_file()` ve el destino; el `stat()` que lo mide falla con `error`.

    Es la ventana TOCTOU del «skip» tal cual: entre las dos lecturas el fichero
    desaparece —o el disco falla—, que es la carrera que el módulo documenta.
    Con presupuesto de uno, porque un destino que nunca se pudiera medir
    impediría también leer el `bytes` del fichero recién instalado.
    """
    real_is_file = Path.is_file
    real_stat = Path.stat
    calls = {"n": 0}

    def _is_file(self, *args, **kwargs):  # noqa: ANN001
        if self == dest:
            return True  # el fichero estaba ahí hace un instante
        return real_is_file(self, *args, **kwargs)

    def _stat(self, *args, **kwargs):  # noqa: ANN001
        if self == dest and calls["n"] == 0:
            calls["n"] += 1
            raise error
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _is_file)
    monkeypatch.setattr(Path, "stat", _stat)
    return calls


def test_an_io_error_measuring_the_destination_is_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R69: un EIO/ESTALE midiendo el destino no sale de `acquire()` sin informe.

    `Path.is_file()` y `Path.stat()` propagan cualquier `OSError` fuera de
    {ENOENT, ENOTDIR, EBADF, ELOOP}: un EIO midiendo un destino en FUSE/NFS/SMB
    —el escenario que nombran los docstrings del módulo— abortaba la
    adquisición entera, sin `AcquiredEntry`, con `report.failed` vacío y con las
    necesidades siguientes sin intentar (el síntoma de R13/R22). El destino no
    está verificado en disco, así que se baja y se verifica: es lo único que
    arregla un destino que no se puede medir.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    calls = _break_dest_stat_once(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert calls["n"] == 1, "el destino se midió una sola vez, y sin propagar"
    assert downloads["n"] == 1, "lo que no se pudo comprobar se baja"
    assert dest.read_bytes() == FAKE_CONTENT
    assert report.failed == []


def test_a_destination_that_vanishes_between_the_two_reads_is_not_an_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R69: la carrera entre el `is_file()` y el `stat()` tampoco escapa.

    El «skip» leía el destino dos veces —para decidir y otra vez para el `bytes`
    del informe— y el fichero podía desaparecer entre ambas: el segundo `stat()`
    lanzaba `FileNotFoundError` fuera de `acquire()`, sin informe y con las
    necesidades siguientes sin intentar. Ahora se mide una sola vez y dentro de
    un `try`, así que la carrera es «no se pudo comprobar».
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"version anterior")
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    calls = _race_the_destination_away(
        monkeypatch,
        dest,
        FileNotFoundError(errno.ENOENT, "No such file or directory", str(dest)),
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert calls["n"] == 1
    assert downloads["n"] == 1
    assert dest.read_bytes() == FAKE_CONTENT, "el activo bueno sustituye al previo"
    assert report.failed == []


def test_a_directory_at_the_destination_is_not_taken_for_the_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R69: el «skip» sigue exigiendo un fichero regular.

    `_dest_size` responde `None` cuando lo que hay en el destino no es un
    fichero. Medir sin más el tamaño daría por bueno un directorio —su
    `st_size` no es 0— y el activo no se descargaría nunca. El fallo que se
    reporta es el de la instalación, que es el que describe lo que pasó.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "failed", "un directorio no es el activo descargado"
    assert "no se pudo instalar" in (entry.error or ""), entry.error
    assert dest.is_dir(), "el directorio del destino se deja como estaba"
    assert not any(dest.iterdir()), "y no se llena de basura"


# --- R70: cancelar también se respeta antes del backoff de la descarga --------


def test_a_cancelled_job_does_not_burn_the_download_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R70: un cancelado entre la descarga fallida y el reintento es un cancelado.

    El sondeo de `should_cancel` vivía en `_run_hf_download` —que ya terminó
    cuando el veredicto es de reintento— y en el camino de instalación. El
    backoff de la descarga se quemaba igual: el job cancelado esperaba 2 s/5 s/
    11 s y acababa en `report.failed`, que el agente lee como «la descarga no
    completó todos los archivos del grupo» en lugar de como cancelación.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    def _failing_client(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        return 1  # rc de fallo: no hay nada aprovechable, se reintenta

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _failing_client)
    slept = _no_sleep(monkeypatch)

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1  # False al entrar, True tras la descarga fallida

    events: list[tuple[str, str]] = []
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
        should_cancel=_should_cancel,
        progress=lambda filename, state: events.append((filename, state)),
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.error is None, "cancelar no es un fallo"
    assert entry.attempts == 1, "un cancelado no se reintenta"
    assert slept == {}, "ni se quema el backoff esperando"
    assert ("out.bin", "cancelled") in events, "el progreso lo dice también"
    assert report.failed == [], "y el informe no lo cuenta como descarga fallida"
    assert not dest.exists()


# --- R71: «no se pudo medir» no es «el cliente no dejó nada» ------------------


def _break_staged_stat_once(monkeypatch: pytest.MonkeyPatch, stage: Path, name: str) -> None:
    """El primer `stat()` del stageado falla con EIO; el resto va al disco.

    El fallo se arma con presupuesto de uno: un `stat` que fallara siempre
    impediría también verificar la segunda descarga, así que la prueba solo
    cubriría el fallo y no la recuperación.

    D1: el stageado vive bajo el subárbol `job-<pid>-<seq>` de la adquisición,
    cuyo nombre exacto no sabe quien siembra el fallo. Se identifica por el
    **nombre del fichero** más el staging que lo contiene, que es lo que
    distingue al stageado del destino —que en estas pruebas se llama igual—.
    """
    real_stat = Path.stat
    budget = {"eio": 1}

    def _eio(self, *args, **kwargs):  # noqa: ANN001
        if budget["eio"] and _under_stage(self, stage, name):
            budget["eio"] -= 1
            raise OSError(errno.EIO, "Input/output error")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _eio)


def test_an_unmeasurable_staged_file_is_not_blamed_on_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R71: el stageado que no se mide se reporta como inverificable.

    `_staged_is_present` colapsaba «no está» y «no se pudo medir» en el mismo
    `False`, así que un rc 0 con un EIO midiendo el stageado se reportaba como
    «el cliente no dejó el activo (no hay fichero)», con su `warning` —el
    fichero estaba ahí, falló el disco— y el operador repetía una descarga que
    iba a fallar igual. El motivo ahora es el que `_verify_integrity` ya usaba
    para el mismo fallo, y el `warning` no se emite: nada demuestra que el
    cliente no dejara nada.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_staged_stat_once(monkeypatch, stage, "out.bin")

    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            max_attempts=1,
        )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo verificar el stageado" in (entry.error or ""), entry.error
    assert "cliente" not in (entry.error or ""), entry.error
    assert not [r for r in caplog.records if "no dejó" in r.getMessage()], (
        "un stageado que no se pudo medir no se le atribuye al cliente"
    )
    assert downloads["n"] == 1
    assert not list(stage.rglob("out.bin")), "el fallo definitivo limpia staging"
    assert not dest.exists()


def test_an_unmeasurable_staged_file_is_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R71: lo que no se pudo comprobar se reintenta, no se da por perdido.

    El veredicto sigue siendo `retryable`, como el de `_verify_integrity`: un
    EIO puntual midiendo el stageado no es evidencia de que el activo esté
    corrupto. El test de al lado fija el motivo con `max_attempts=1`; este fija
    que la adquisición se recupera cuando el disco responde.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_staged_stat_once(monkeypatch, stage, "out.bin")

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.attempts == 2
    assert downloads["n"] == 2, "un stageado inmedible se vuelve a bajar"
    assert dest.read_bytes() == FAKE_CONTENT


# --- R72: medir el destino instalado no es instalar ---------------------------


def _break_dest_stat_forever(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> None:
    """Ningún `stat()` del destino llega al disco: el disco falla siempre.

    Es el destino cuyo `stat()` devuelve EIO/ESTALE de forma permanente
    (FUSE/NFS/SMB). Ni la comprobación previa a la descarga ni la posterior al
    `rename` pueden medirlo, y **ninguna** de las dos es un fallo de instalación.
    """
    real_stat = Path.stat

    def _stat(self, *args, **kwargs):  # noqa: ANN001
        if self == dest:
            raise error
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)


def _break_dest_stat_after_install(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> dict[str, int]:
    """El primer `stat()` del destino **después** del rename falla con `error`.

    El contador se arma en `_move`, no con un `n == 0` pelado, porque la
    comprobación previa a la descarga también mide el destino —en CPython
    `is_file()` llama a `stat()`— y un contador pelado se gastaría ahí, antes del
    rename, que es la ventana de R69 y no la de R72. Aquí la medida que falla es
    la que sucede cuando el destino **ya está publicado**.
    """
    real_move = asset_acquire._move
    real_stat = Path.stat
    state = {"armed": False, "n": 0}

    def _arming_move(src: Path, target: Path) -> None:
        state["armed"] = True  # el rename está a punto de publicar el destino
        return real_move(src, target)

    def _stat(self, *args, **kwargs):  # noqa: ANN001
        if self == dest and state["armed"] and state["n"] == 0:
            state["n"] += 1
            raise error
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(asset_acquire, "_move", _arming_move)
    monkeypatch.setattr(Path, "stat", _stat)
    return state


def test_a_destination_that_cannot_be_measured_after_installing_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R72: un EIO midiendo el destino instalado no es un fallo de instalación.

    La medida vivía dentro del `try` del `rename`, así que un `OSError` después
    de un `_move` exitoso se reportaba como «no se pudo instalar»: el activo
    estaba en disco y correcto, y el informe mentía sobre lo que pasó. La pasada
    siguiente reentraba por la comprobación previa al rename —el stageado ya se
    había consumido—, la desactivaba y **volvía a descargar**, contradiciendo el
    `warning` que acababa de prometer «se reintenta el rename sin volver a
    descargar».
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT), (
        "el destino no se midió, pero el stageado sí: se informa su tamaño "
        "en lugar de inventarse un 0"
    )
    assert "no se pudo medir el destino instalado" in (entry.error or ""), entry.error
    assert "no se pudo instalar" not in (entry.error or ""), (
        "el rename funcionó: el fallo de medida no se le atribuye"
    )
    assert downloads["n"] == 1, "un activo ya instalado no se vuelve a descargar"
    assert slept == {}, "ni se quema el backoff reintentando un rename ya hecho"
    assert dest.read_bytes() == FAKE_CONTENT, "el activo está instalado y correcto"
    assert report.failed == []


def test_a_second_acquire_of_an_unmeasurable_destination_terminates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R72/R77: un destino que no se puede medir no convertía cada pasa en un fallo.

    Con la medida dentro del `try`, cada `acquire` medía mal el destino, se lo
    atribuía a la instalación, lo volvía a bajar y acababa en `failed` borrando
    la caché del cliente: un fallo permanente autoinfligido sobre un activo de
    GBs que lleva todo ese tiempo en disco. Ahora termina, y además **reutiliza**:
    el destino no se deja medir, pero este módulo lo instaló y lo verificó, así
    que queda registrado en `<dest>.installed` y la segunda adquisición lo
    reconoce como presente en lugar de bajarlo otra vez (R77).
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )
    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    assert first.failed == [] and second.failed == [], "ninguna pasa acaba en fallo"
    assert first.entries[0].status == "downloaded", first.entries[0].error
    assert "no se pudo instalar" not in (first.entries[0].error or "")
    assert downloads["n"] == 1, (
        "una sola descarga: el destino instalado y verificado se reutiliza"
    )
    assert dest.read_bytes() == FAKE_CONTENT, "el activo sigue instalado y correcto"
    assert (dest.parent / "out.bin.installed").is_file(), "la marca quedó en disco"

    skipped = second.entries[0]
    assert skipped.status == "skipped", skipped.error
    assert skipped.bytes == len(FAKE_CONTENT), "el tamaño se sabe por el stageado"
    assert skipped.error is None, "«ya está en disco» no es un fallo ni un pendiente"
    assert downloads["n"] == 1, "y la segunda adquisición no baja nada"


def _break_dest_stat_until(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> dict[str, bool]:
    """Ningún `stat()` del destino llega al disco hasta que se diga lo contrario."""
    real_stat = Path.stat
    state = {"broken": True}

    def _stat(self, *args, **kwargs):  # noqa: ANN001
        if state["broken"] and self == dest:
            raise error
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat)
    return state


def test_the_installed_marker_is_dropped_once_the_destination_is_measurable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R77: la marca de «instalado» no puede dar por bueno un destino medible.

    La marca existe para un destino cuyo `stat()` falla siempre. Si el disco
    vuelve a responder y el fichero **no** es el declarado, la marca no puede
    seguir justificando el salto: quien mide es `_inspect_dest`, que compara
    tamaño y hash, y un destino medible que no cuadra se vuelve a descargar.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    broken = _break_dest_stat_until(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )
    assert first.entries[0].status == "downloaded", first.entries[0].error
    assert downloads["n"] == 1
    assert (dest.parent / "out.bin.installed").is_file(), "la marca quedó en disco"

    # El disco responde otra vez, y lo que hay en el destino no es el activo:
    # mismo tamaño, contenido distinto. La marca no puede salvarlo.
    broken["broken"] = False
    dest.write_bytes(b"otros!")

    again = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = again.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 2, "un destino medible que no cuadra se re-descarga"
    assert dest.read_bytes() == FAKE_CONTENT, "el activo bueno sustituye al previo"
    assert not (dest.parent / "out.bin.installed").exists(), (
        "la marca se retira en cuanto el destino se mide"
    )


def test_an_io_error_on_the_first_post_install_measurement_is_not_an_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R72: ni siquiera un EIO **puntual** tras el rename se trata como instalación.

    Es el caso del ayudante de R69, que rompía el primer `stat()` del destino
    con presupuesto de uno a propósito para no cubrir el camino de fallo de
    instalación. Con la medida fuera del `try`, el EIO puntual se informa como
    tamaño desconocido y la adquisición siguiente —con el disco ya respondiendo—
    reconoce el activo en disco y lo salta.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)
    state = _break_dest_stat_after_install(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert "no se pudo medir el destino instalado" in (entry.error or ""), entry.error
    assert "no se pudo instalar" not in (entry.error or ""), entry.error
    assert state["n"] == 1, "el EIO se consumió midiendo el destino ya instalado"
    assert downloads["n"] == 1, "no se re-descarga por un fallo de medida"
    assert slept == {}
    assert dest.read_bytes() == FAKE_CONTENT

    again = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    skipped = again.entries[0]
    assert skipped.status == "skipped", skipped.error
    assert skipped.bytes == len(FAKE_CONTENT), "el activo se reconoce en disco"
    assert downloads["n"] == 1, "y no se vuelve a bajar"


# --- R73: el presupuesto de intentos no se adelanta a la cancelación ----------


def test_a_job_cancelled_on_the_last_attempt_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R73: un cancelado en el último intento es un cancelado, no un fallo.

    El sondeo de `should_cancel` vivía **después** del `attempt >= max_attempts`,
    así que era el único camino de cancelación que el presupuesto podía
    adelantar: con `max_attempts=1` —y con cualquier presupuesto, en la última
    pasada— un job cancelado acababa en `report.failed`, que el agente lee como
    «la descarga no completó todos los archivos del grupo». Los otros dos
    caminos (el rc 130 y el reintento del `rename`) devuelven `cancelled` sin
    mirar el presupuesto.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    def _failing_client(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        return 1  # rc de fallo: el veredicto es de reintento

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _failing_client)
    slept = _no_sleep(monkeypatch)

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1  # False al entrar, True tras la descarga fallida

    events: list[tuple[str, str]] = []
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
        should_cancel=_should_cancel,
        progress=lambda filename, state: events.append((filename, state)),
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.error is None, "cancelar no es un fallo"
    assert entry.attempts == 1, "un cancelado no se reintenta"
    assert slept == {}, "ni se quema el backoff que ya no sirve de nada"
    assert ("out.bin", "cancelled") in events, "el progreso lo dice también"
    assert report.failed == [], "y el informe no lo cuenta como descarga fallida"
    assert not dest.exists()


# --- R74: `_dest_size` mide; el filtrado del vacío es del llamador ------------


def test_the_size_of_an_empty_destination_is_zero_not_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R74: medir y filtrar no es lo mismo, y el docstring ya no promete lo otro.

    `_dest_size` devolvía el `st_size` tal cual mientras el docstring anunciaba
    «fichero regular no vacío», así que un destino vacío daba `0` —medido— allí
    donde el lector esperaba `None` —no medido—. Ahora el docstring dice lo que
    hace: mide, y un fichero vacío medido es `0`. El filtro `> 0` sigue vivo en
    el llamador, que es quien decide que un destino vacío se vuelve a descargar.
    """
    empty = tmp_path / "vacio.bin"
    empty.write_bytes(b"")
    full = tmp_path / "lleno.bin"
    full.write_bytes(FAKE_CONTENT)
    folder = tmp_path / "directorio"
    folder.mkdir()

    assert asset_acquire._dest_size(empty) == 0, "vacío y medido es 0, no «no sé»"
    assert asset_acquire._dest_size(full) == len(FAKE_CONTENT)
    assert asset_acquire._dest_size(tmp_path / "no-existe.bin") is None
    assert asset_acquire._dest_size(folder) is None, "un directorio no es el activo"

    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"")  # un activo a medias de una pasada anterior
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "el `> 0` del llamador es el que descarta el vacío"
    assert dest.read_bytes() == FAKE_CONTENT


# --- R75: «no se pudo comprobar» y «desapareció» son mensajes distintos -------


def _break_staged_presence_after_the_first(
    monkeypatch: pytest.MonkeyPatch, stage: Path, name: str
) -> dict[str, int]:
    """El segundo `is_file()` del stageado falla con EIO; el primero va al disco.

    El primero es el de `_check_staged`, que tiene que pasar para llegar a la
    comprobación previa al `rename`; el segundo es esa. Romper el primero
    dejaría la prueba en el veredicto de la descarga, no en el de R71. En CPython
    `is_file()` llama a `stat()`, así que romper `Path.is_file` basta para que
    `_staged_is_present` responda `None`.

    D1: el stageado se busca por nombre de fichero dentro del staging, no por
    ruta completa: vive bajo el subárbol `job-<pid>-<seq>` de la adquisición.
    """
    real_is_file = Path.is_file
    calls = {"n": 0}

    def _is_file(self, *args, **kwargs):  # noqa: ANN001
        if _under_stage(self, stage, name):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError(errno.EIO, "Input/output error")
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _is_file)
    return calls


def test_an_unmeasurable_staged_file_before_the_install_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R71/R75: «no se pudo comprobar» sigue siendo distinto sin intentos.

    La prueba de al lado del módulo solo cubría la rama con intentos —el
    `continue` que vuelve a descargar—, así que el mensaje terminal y su
    diferencia con el de «desapareció» quedaban sin fijar. Decir «desapareció»
    cuando falló el disco manda a buscar una retención que no tuvo nada que ver.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_staged_presence_after_the_first(monkeypatch, stage, "out.bin")

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo comprobar el stageado" in (entry.error or ""), entry.error
    assert "antes de instalarlo" in (entry.error or ""), entry.error
    assert "desapareció" not in (entry.error or ""), (
        "el stageado estaba: lo que falló fue la medida"
    )
    assert entry.attempts == 1
    assert downloads["n"] == 1
    assert not list(stage.rglob("out.bin")), "el fallo definitivo limpia staging"
    assert not dest.exists()


def test_a_staged_file_that_vanishes_before_the_install_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R35/R75: el stageado perdido **sí** se dice como desaparecido.

    Es el otro mensaje de la misma comprobación, y conviene que quede fijado
    junto al de al lado: mismo sitio, misma decisión de no renombrar a ciegas,
    pero la noticia es otra —aquí sí hay un fichero que se fue—.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    def _vanish_after_verify(path: Path, need: object) -> bool:
        # La retención se lleva el stageado recién verificado, entre la
        # comprobación y el `rename`.
        path.unlink()
        return True

    monkeypatch.setattr(asset_acquire, "_verify_integrity", _vanish_after_verify)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "desapareció antes de instalarlo" in (entry.error or ""), entry.error
    assert "no se pudo comprobar" not in (entry.error or ""), (
        "aquí el stageado se midió: estaba y dejó de estar"
    )
    assert entry.attempts == 1
    assert downloads["n"] == 1
    assert not list(stage.rglob("out.bin")), "el fallo definitivo limpia staging"
    assert not dest.exists()


# --- R76: ningún parcial puede parecer completo ---------------------------------


def _counting_digest(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """`_raw_digest` que cuenta cuántas veces se ha data el contenido.

    Es el contador que distingue «verificado» de «dado por bueno»: si el «skip»
    no llama a esta función, el destino no se ha comprobado.
    """
    real_digest = asset_acquire._raw_digest
    hashes: dict[str, int] = {}

    def _digest(path: Path, algorithm: str) -> str:
        hashes["n"] = hashes.get("n", 0) + 1
        return real_digest(path, algorithm)

    monkeypatch.setattr(asset_acquire, "_raw_digest", _digest)
    return hashes


def test_a_truncated_destination_is_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: un destino truncado se vuelve a bajar, no se da por bueno.

    El «skip» aceptaba cualquier destino no vacío: 19 bytes de un activo de 20 GB
    se reportaban `skipped` con `bytes=19` y ningún error, para siempre. Es la
    única vía por la que un parcial podía parecer completo desde el punto de
    vista de este módulo —la compartía con `scripts/download-models.sh`, que
    también se conforma con `[ -s ]`—, así que una descarga interrumpida por
    cualquier otro escritor dejaba un activo que nadie iba a reparar.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT[:3])  # truncado: ni el tamaño ni el hash cuadran
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "el destino truncado se repara en lugar de saltarse"
    assert dest.read_bytes() == FAKE_CONTENT
    assert entry.bytes == len(FAKE_CONTENT)


def test_a_correct_destination_is_skipped_after_hashing_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: el destino correcto se salta, y solo se hasea cuando hace falta.

    Comprobar el contenido en cada `acquire` sería inaceptable —20 GB por
    adquisición—, así que la verificación se cachea en un sidecar junto al
    destino indexado por `(mtime, size)`. La primera adquisición haea una vez y
    fija la caché; las siguientes no leen un byte. El hash se cuenta para
    probarlo.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    hashes = _counting_digest(monkeypatch)

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    for report in (first, second):
        entry = report.entries[0]
        assert entry.status == "skipped", entry.error
        assert entry.bytes == len(FAKE_CONTENT)
        assert entry.error is None, "un destino verificado no es un fallo"
    assert downloads.get("n", 0) == 0, (
        "nada se descarga: el activo ya está y es el declarado"
    )
    assert hashes["n"] == 1, "el hash se calculó una vez, no una por adquisición"
    assert (dest.parent / "out.bin.integrity").is_file(), "la caché quedó en disco"


def test_a_destination_whose_content_changed_but_not_its_size_is_caught(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: un cambio de contenido que conserva el tamaño invalida la caché.

    La caché se indexa por `(mtime, size)`: si el tamaño es el mismo pero el
    mtime cambia, el contenido ya no es el que se comprobó. Sin esa invalidación
    un activo sobrescrito por otro del mismo tamaño pasaría por el declarado para
    siempre, que es el mismo agujero que R76 pero por el lado de la caché.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    hashes = _counting_digest(monkeypatch)

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    assert first.entries[0].status == "skipped"
    assert hashes["n"] == 1

    # Mismo tamaño, contenido distinto y un mtime explícitamente otro: la caché
    # caduca y el hash vuelve a calcularse.
    stat = dest.stat()
    dest.write_bytes(b"XXXXX")
    assert dest.stat().st_size == len(FAKE_CONTENT), "el tamaño no cambia"
    os.utime(dest, ns=(stat.st_mtime_ns + 10**9, stat.st_mtime_ns + 10**9))

    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = second.entries[0]
    assert entry.status == "downloaded", entry.error
    assert hashes["n"] == 3, (
        "la caché caducó: un hash para descubrirlo y otro para verificar "
        "la descarga que arregla el destino"
    )
    assert downloads["n"] == 1, "y lo que no cuadra se re-descarga"
    assert dest.read_bytes() == FAKE_CONTENT


def test_a_freshly_installed_asset_is_never_hashed_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: el caso común —nada ha cambiado— no hasea nada.

    La instalación conoce el hash: `_check_staged` lo calculó sobre el stageado
    para verificar la descarga, y ese mismo contenido es el que se publica. Con
    eso se fija la caché sin volver a leer 20 GB, así que una adquisición
    posterior sobre un activo recién instalado no hasea absolutamente nada.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    hashes = _counting_digest(monkeypatch)

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    assert first.entries[0].status == "downloaded", first.entries[0].error
    assert second.entries[0].status == "skipped", second.entries[0].error
    assert downloads["n"] == 1
    assert hashes["n"] == 1, (
        "un solo hash en total: el de verificar la descarga, y ninguno más"
    )
    assert (dest.parent / "out.bin.integrity").is_file()


def test_a_destination_whose_hash_does_not_match_is_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: un activo instalado y corrupto se repara, no se respeta para siempre.

    El mismo agujero de R76 por el lado del contenido: un destino del tamaño
    declarado pero con los bytes cambiados se saltaba sin comprobar nada. Con el
    hash declarado, se re-descarga.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"XXXXX")  # mismo tamaño, contenido distinto
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "el activo corrupto se repara"
    assert dest.read_bytes() == FAKE_CONTENT


def test_a_restored_mtime_defeats_the_integrity_cache_and_that_is_documented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R88: el límite conocido de la cache de integridad, por escrito.

    La cache se indexa por `(mtime, size)`, así que da por bueno un contenido
    distinto del verificado si alguien **restaura** el `mtime`: es lo que hacen
    `cp -p`, `rsync -a` y el restore de una copia de seguridad. Esta prueba
    fija ese comportamiento en lugar de la garantía que el docstring anterior
    prometía («un cambio de contenido que conserva el tamaño cambia el mtime»),
    que es falsa justo en el caso que aquí se reproduce.

    No es un agujero que se pueda cerrar sin hasear en cada adquisición, que es
    lo que la cache existe para no hacer: la garantía de integridad la sigue
    dando el hash, que se calcula siempre que el tamaño o el mtime se mueven
    (`test_a_destination_whose_content_changed_but_not_its_size_is_caught`).
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    hashes = _counting_digest(monkeypatch)

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    assert first.entries[0].status == "skipped", first.entries[0].error
    assert hashes["n"] == 1

    # Mismo tamaño, contenido distinto y el `mtime_ns` restaurado al original:
    # la cache no puede distinguirlo del activo que comprobó.
    stat = dest.stat()
    dest.write_bytes(b"XXXXX")
    assert dest.stat().st_size == len(FAKE_CONTENT), "el tamaño no cambia"
    os.utime(dest, ns=(stat.st_mtime_ns, stat.st_mtime_ns))

    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = second.entries[0]
    assert entry.status == "skipped", (
        "límite conocido: con el mtime restaurado la cache da el destino por "
        "bueno sin volver a hasearlo"
    )
    assert hashes["n"] == 1, "no se ha leído un byte: la cache sigue vigente"
    assert downloads.get("n", 0) == 0
    assert dest.read_bytes() == b"XXXXX", (
        "y el contenido corrupto sigue en disco: es el precio de no hasear "
        "20 GB en cada adquisición"
    )


def test_a_destination_without_a_declared_hash_is_skipped_on_its_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: sin hash no se inventa uno —«where available»—, pero el tamaño manda.

    La política exige comprobar la integridad «donde esté disponible». Un
    manifiesto que solo declara bytes se sigue saltando por el tamaño, que es la
    única red que tiene; lo que no puede es saltarse un destino cuyo tamaño no
    declara nada.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    hashes = _counting_digest(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert hashes.get("n", 0) == 0, "sin hash declarado no se hasea"
    assert downloads.get("n", 0) == 0
    assert not (dest.parent / "out.bin.integrity").exists(), (
        "ni se escribe una caché de un hash que no existe"
    )

    # Y un destino cuyo tamaño no declara el manifiesto se vuelve a bajar: sin
    # hash, el tamaño es la única red que queda.
    dest.write_bytes(FAKE_CONTENT[:3])
    other = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
    )
    again = asset_acquire.acquire(
        [other],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    assert again.entries[0].status == "downloaded", again.entries[0].error
    assert downloads.get("n", 0) == 1
    assert dest.read_bytes() == FAKE_CONTENT


# --- R91: sin hash y sin tamaño no hay nada que verificar ---------------------


def test_a_hashless_and_sizeless_manifest_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R91: sin hash y sin tamaño utilizable, ningún parcial parece completo.

    `_verify_integrity` acepta un activo sin hash («where available») y la red
    del tamaño estaba condicionada a que `expected` fuera verdadero, donde
    `expected = _declared_bytes(need)` vale 0 **tanto** cuando el manifiesto
    omite `bytes` como cuando lo declara ilegible. Un cliente que dejaba la
    mitad del fichero publicaba entonces `status='downloaded'` con el truncado
    ya en el destino: el invariante que gobierna el módulo, incumplido. Ahora
    se falla cerrado, y sin reintento —el problema es el manifiesto—.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=0,
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire,
        "_run_hf_download",
        _counting_download(FAKE_CONTENT[:7], downloads),
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert entry.error == asset_acquire.UNVERIFIABLE, entry.error
    assert entry.attempts == 1, "no se reintenta: no es la descarga lo que falla"
    assert downloads["n"] == 1
    assert not dest.exists(), "el parcial no se publica"
    assert not list(stage.rglob("out.bin")), "ni se deja en staging"


def test_an_unreadable_size_with_no_hash_also_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R91: `"bytes": "muchos"` es «no declarado», así que tampoco hay red.

    Es el otro origen del mismo 0: `_declared_bytes` degrada un tamaño
    ilegible a 0 con un `warning`, y ese 0 era indistinguible de «el manifiesto
    no declara tamaño». Sin hash, el resultado es el mismo fail-closed.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=0,
        remote_path="out.bin",
    )
    need.bytes = "muchos"  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire,
        "_run_hf_download",
        _counting_download(FAKE_CONTENT[:7], downloads),
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert entry.error == asset_acquire.UNVERIFIABLE, entry.error
    assert downloads["n"] == 1
    assert not dest.exists()
    assert not list(stage.rglob("out.bin"))


def test_a_hashless_asset_with_a_declared_size_still_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R91 (regresión): el tamaño declarado sigue siendo red suficiente.

    El fail-closed no puede llevarse por delante los activos que HuggingFace
    sirve solo por tamaño: con `bytes` declarado, la comprobación es la de
    siempre y un fichero completo se instala.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert dest.read_bytes() == FAKE_CONTENT
    # Y un truncado contra el tamaño declarado se sigue rechazando.
    truncated = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
    )
    dest.write_bytes(FAKE_CONTENT[:3])
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT[:3], downloads)
    )
    again = asset_acquire.acquire(
        [truncated],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )
    assert again.entries[0].status == "failed", again.entries[0].error
    assert "tamano" in (again.entries[0].error or ""), again.entries[0].error


def test_a_hashless_and_sizeless_destination_is_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R91: la misma regla en el lado de la reutilización (`_inspect_dest`).

    Sin hash y sin tamaño, un destino ya presente se daba por bueno sin comprobar
    nada. Ahora se vuelve a descargar, que es lo conservador.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=0,
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    verdict = asset_acquire._inspect_dest(dest, need)
    assert verdict.reusable is False, "sin hash ni tamaño el destino no se reutiliza"
    assert verdict.reason == asset_acquire.UNVERIFIABLE, verdict.reason

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert downloads["n"] == 1, "se vuelve a bajar en lugar de darlo por bueno"
    # Y la propia descarga falla cerrada también: es la misma necesidad.
    assert entry.status == "failed", entry.error
    assert entry.error == asset_acquire.UNVERIFIABLE, entry.error


# --- R92: un destino sin nombre de fichero es una entrada, no una excepción ---


# --- R95: el camino sin cliente también consulta la cancelación --------------


@pytest.mark.parametrize("hf_cli", [None, "/fake/hf\x00"])
def test_a_cancelled_job_without_a_usable_client_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hf_cli: str | None
) -> None:
    """R95: sin cliente (o con uno inservible) un cancelado seguía siendo `failed`.

    Ese camino construía el `AcquiredEntry` a mano, sin pasar por
    `_failure_entry`, así que no consultaba `should_cancel`: un job cancelado
    acababa en `report.failed` con `cancelled=False`, y el agente lo lee como
    «la descarga no completó todos los archivos del grupo». Es el residuo de R79
    que rompía «cancelar manda siempre ... un solo sitio».
    """
    needs = [
        _acquire_need(
            filename="m0.bin",
            dest_path=str(tmp_path / "m0.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="m0.bin",
            sha256=_digest(),
        )
    ]
    if hf_cli is None:
        monkeypatch.setattr(asset_acquire, "resolve_hf_cli", lambda env=None: None)

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli=hf_cli,
        stage_dir=tmp_path / "stage",
        should_cancel=lambda: True,
    )

    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.error is None, "una cancelación no lleva motivo de fallo"
    assert report.cancelled is True
    assert report.failed == [], "el cancelado no entra en la lista de fallos"


# --- R92: un destino sin nombre de fichero es una entrada, no una excepción ---


@pytest.mark.parametrize("dest_path", ["/", "/.", "//", "/.."])
def test_a_destination_without_a_final_component_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dest_path: str
) -> None:
    """R92/D6: `/`, `/.`, `//` y `/..` abortaban `acquire()` con `ValueError`.

    `Path('/')` se construye sin problemas, pero `_sidecar` hacía
    `dest.with_name(dest.name + suffix)` sobre un nombre vacío y `pathlib`
    responde `ValueError: PosixPath(/) has an empty name`. La excepción escapaba
    de `_acquire_one` y de `acquire()`, sin informe y con las necesidades
    siguientes sin intentar. Es alcanzable desde un manifiesto de operario,
    porque `model_assets.resolve_dest` respeta una ruta absoluta tal cual.

    D6: `/..` entraba por el agujero de `not dest.name` —`Path('/..').name` es
    `'..'`, que no está vacío—, así que llegaba a `_drop_stale_partials` y a
    `_move` con la raíz como directorio de destino. Se rechaza con los otros
    tres: ninguno de los cuatro es un nombre de fichero.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=dest_path,
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert "destino sin nombre de fichero" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"
    assert not stage.exists() or not list(stage.rglob("*")), "no se escribe nada"


# --- R97: lo que no es una ruta es una entrada, no una excepción --------------


@pytest.mark.parametrize("bad_path", [["/models/out.bin"], {"p": 1}, 7, b"/models/x"])
def test_a_dest_path_that_is_not_a_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_path: object
) -> None:
    """R97: `Path()` sobre una lista o un entero responde `TypeError`.

    No es alcanzable desde un manifiesto (`capabilities._asset_need` coerciona
    `dest_path` con `str()`), pero sí desde cualquier llamador directo de
    `acquire()`, y el `TypeError` no lo cubría ningún `except (OSError,
    ValueError)` del módulo: el plan abortaba sin informe. Se rechaza donde ya
    se validaban los caracteres de control.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = bad_path  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert "dest_path no es una ruta" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"


@pytest.mark.parametrize("bad_cli", [7, ["/fake/hf"], {"cli": 1}])
def test_an_hf_cli_that_is_not_a_path_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_cli: object
) -> None:
    """R97: un `hf_cli` que no es una ruta llegaba a `subprocess.Popen`.

    Su `TypeError` no está cubierto por el `except (OSError, ValueError)` de la
    descarga, así que la excepción salía de `acquire()` sin informe. Se rechaza
    con el resto de los problemas del cliente, una entrada por necesidad.
    """
    needs = [
        _acquire_need(
            filename="m0.bin",
            dest_path=str(tmp_path / "m0.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="m0.bin",
            sha256=_digest(),
        )
    ]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli=bad_cli,  # type: ignore[arg-type]
        stage_dir=tmp_path / "stage",
    )

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert "HF_CLI no es una ruta" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0


def test_a_path_like_dest_path_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R97 (regresión): un `os.PathLike` sigue siendo un destino válido.

    El tipo exigido es `str | os.PathLike`, no `str`: quien llama con un `Path`
    no puede recibir una entrada de fallo por ello.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = dest  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert dest.read_bytes() == FAKE_CONTENT


def test_a_dest_path_that_is_a_bytes_path_like_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R97 (residual): un `PathLike` con `__fspath__` en bytes era una excepción.

    Mismo hueco que el `dest_path` que no es una ruta, una forma más abajo: el
    tipo admite `os.PathLike`, y `PathLike` solo promete un `__fspath__` que
    puede devolver `bytes`. `Path(...)` sobre eso responde `TypeError` sin que
    ningún `except (OSError, ValueError)` del módulo lo vea, así que `acquire()`
    abortaba con el plan entero sin intentar y sin informe. Se rechaza donde ya
    se validan los caracteres de control.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"

    class _BytesPathLike:
        def __fspath__(self) -> bytes:
            return bytes(dest)

    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = _BytesPathLike()  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert "dest_path no es una ruta" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"
    assert not dest.exists(), "no se instala nada"


# --- D5: los kwargs tipados de `acquire()` tampoco abortan el plan -------------


@pytest.mark.parametrize(
    ("kwarg", "value"),
    [
        ("stage_dir", 7),
        ("stage_dir", ["/tmp"]),
        ("stage_dir", 1.5),
        ("margin_bytes", "abc"),
        ("margin_bytes", [1]),
        ("margin_bytes", float("inf")),
        ("timeout_s", "3600"),
        ("timeout_s", float("inf")),
        ("timeout_s", -1.5),
        ("max_attempts", "2"),
        ("max_attempts", float("inf")),
        ("max_attempts", None),
        ("max_attempts", 1.5),
        ("env", ["A=1"]),
        ("env", 7),
        ("env", "A=1"),
        ("should_cancel", 7),
        ("should_cancel", "si"),
    ],
)
def test_a_badly_typed_acquire_kwarg_becomes_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwarg: str, value: object
) -> None:
    """D5: un kwarg que no es del tipo que dice su firma es una entrada.

    R97 blindó `dest_path` y `hf_cli`, y `_env_int` blindó las variables de
    entorno equivalentes; los kwargs tipados de `acquire()` quedaban **más
    débiles que sus propias variables**: `HF_FETCH_TIMEOUT=inf` degradaba al
    valor documentado, pero `timeout_s=float('inf')` llegaba a
    `time.monotonic() + inf`, `stage_dir=7` a `Path(7)`, `env=['A=1']` a
    `environment.get(...)` y `should_cancel=7` a la llamada del sondeo. Todos
    respondían `TypeError`/`AttributeError`, fuera de todo `except (OSError,
    ValueError)`: el plan abortaba sin informe y con las necesidades siguientes
    sin intentar.

    Ahora se validan al entrar, con las mismas reglas, y cada necesidad recibe
    su entrada de fallo: quien llama puede leer el informe en lugar de cazar una
    excepción.
    """
    stage = tmp_path / "stage"
    needs = [
        _acquire_need(
            filename="out.bin",
            dest_path=str(tmp_path / "out.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="out.bin",
            sha256=_digest(),
        ),
        _acquire_need(
            filename="otro.bin",
            dest_path=str(tmp_path / "otro.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="otro.bin",
            sha256=_digest(),
        ),
    ]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    kwargs: dict[str, object] = {
        "env": {"HF_DISK_GUARD": "0"},
        "hf_cli": "/fake/hf",
        "stage_dir": stage,
        "max_attempts": 1,
        kwarg: value,
    }

    report = asset_acquire.acquire(needs, **kwargs)  # type: ignore[arg-type]

    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"
    assert [e.filename for e in report.entries] == ["out.bin", "otro.bin"], kwarg
    for entry in report.entries:
        assert entry.status == "failed", (kwarg, value, entry.error)
        assert kwarg in (entry.error or ""), (kwarg, value, entry.error)
    assert report.failed, "el informe dice qué falló"


def test_a_cancelled_job_with_a_bad_kwarg_is_still_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D5: cancelar manda también en el camino de los kwargs (R79).

    El rechazo construye entradas con `_failure_entry`, así que un job cancelado
    se reporta `cancelled` y no entra en `report.failed`. `should_cancel` es
    válido en esta prueba; el que se rechaza es `max_attempts`.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts="2",
        should_cancel=lambda: True,
    )

    assert report.entries[0].status == "cancelled", report.entries[0].error
    assert report.cancelled is True
    assert report.failed == [], "un cancelado no es una descarga fallida"
    assert downloads.get("n", 0) == 0


def test_the_sidecar_of_a_nameless_destination_does_not_raise(tmp_path: Path) -> None:
    """R92: `_sidecar` construye sobre el padre, que en una ruta siempre existe.

    Es la red que queda para los llamadores que no pasan por `_acquire_one`: el
    `with_name` sobre un nombre vacío era un `ValueError`, y aquí no lo es.
    """
    assert asset_acquire._sidecar(Path("/"), ".integrity") == Path("/.integrity")


# --- D6: el mismo patrón en `_move` y `_remove_partials` -----------------------


def test_move_to_a_nameless_destination_is_an_os_error_not_a_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6: `_move` también construye su temporal sobre el padre.

    R92 arregló `_sidecar`, pero `_move` conservaba
    `dest.with_name(f"{dest.name}{PARTIAL_MARK}...")`, que responde
    `ValueError: PosixPath(/) has an empty name`. Inalcanzable desde
    `acquire()` —la guarda de `_acquire_one` rechaza el destino antes—, pero
    `_move` la llaman directamente y un `ValueError` ahí no es un fallo de
    instalación: es una excepción sin informe.

    Para llegar al temporal hay que caer en el camino cross-device, porque
    `os.replace` se intenta primero y con la raíz como destino responde `OSError`
    por su cuenta.
    """
    src = tmp_path / "src.bin"
    src.write_bytes(b"datos")

    def _exdev(a, b):  # noqa: ANN001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", _exdev)

    with pytest.raises(OSError) as raised:
        asset_acquire._move(src, Path("/"))

    assert not isinstance(raised.value, ValueError), raised.value


def test_remove_partials_of_a_nameless_staged_path_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D6/R4: el `.incomplete` heredado se construye sobre el padre.

    `_remove_partials` conservaba el otro `with_name` del módulo:
    `staged.with_name(staged.name + ".incomplete")`, con el mismo `ValueError`
    para un stageado sin nombre. Lo que se comprueba es la construcción del
    sidecar, no la limpieza: `_remove_staged` se queda en su guarda de R4 y
    `shutil.rmtree` se intercepta para que un `Path('/')` no llegue a borrar
    nada (aquí ni en una máquina sin permiso de escritura sobre la raíz).
    """
    rmtree_calls: list[str] = []

    def _recording_rmtree(path, *args, **kwargs):  # noqa: ANN001
        rmtree_calls.append(str(path))

    monkeypatch.setattr(asset_acquire.shutil, "rmtree", _recording_rmtree)
    downloads = tmp_path / ".cache" / "huggingface" / "download"
    downloads.mkdir(parents=True)
    leftover = downloads / "out.bin.incomplete"
    leftover.write_bytes(b"parcial")

    asset_acquire._remove_partials(Path("/"), downloads)

    assert rmtree_calls == [], "un stageado sin nombre no se borra: arrasaría la raíz"
    assert leftover.is_file(), (
        "el barrido es por nombre: sin nombre de stageado no hay nada que casar"
    )


# --- D7: los cuatro ayudantes de la limpieza, probados por separado ------------


def test_is_staged_name_matches_only_the_staged_file_and_its_auxiliaries() -> None:
    """D7: la pertenencia por nombre es exacta, no un prefijo a secas.

    `out.binx` no es de `out.bin`: el punto es lo que separa el nombre del
    fichero de sus auxiliares (`.incomplete`, `.metadata`). Sin él, un activo
    cuyo nombre empieza como otro entraría en el barrido ajeno.
    """
    assert asset_acquire._is_staged_name("out.bin", "out.bin")
    assert asset_acquire._is_staged_name("out.bin.incomplete", "out.bin")
    assert asset_acquire._is_staged_name("out.bin.metadata", "out.bin")
    assert not asset_acquire._is_staged_name("out.binx", "out.bin")
    assert not asset_acquire._is_staged_name("otro.bin", "out.bin")
    assert not asset_acquire._is_staged_name("out.bin", "out.bin.incomplete")


@pytest.mark.parametrize(
    "value",
    [7, ["/models/out.bin"], {"p": 1}, b"/models/x", 1.5],
)
def test_unsafe_path_rejects_what_is_not_a_path(value: object) -> None:
    """D7: `_unsafe_path` es la guarda de `stage_dir` y de `hf_cli` (R97)."""
    problem = asset_acquire._unsafe_path("stage_dir", value)
    assert problem is not None, value
    assert "no es una ruta" in problem, problem


@pytest.mark.parametrize("value", [None, "/models/out.bin", Path("/models/out.bin")])
def test_unsafe_path_accepts_what_is_a_path(value: object) -> None:
    """D7 (regresión): un `None` y un `os.PathLike` siguen siendo válidos."""
    assert asset_acquire._unsafe_path("stage_dir", value) is None, value


def test_unsafe_path_rejects_a_path_like_whose_fspath_returns_bytes() -> None:
    """R97 (residual): un `os.PathLike` en bytes tampoco es una ruta.

    `os.PathLike` solo promete un `__fspath__`, y ese puede devolver `bytes`.
    `os.fspath` lo devuelve tal cual, pero `Path(...)` responde
    `TypeError: ... where __fspath__ returns a str, not bytes` sobre él —fuera de
    todo `except (OSError, ValueError)`—, con el plan abortado y sin informe. Se
    rechaza donde ya se rechaza lo que no es una ruta.
    """

    class _BytesPathLike:
        def __fspath__(self) -> bytes:
            return b"/models/out.bin"

    problem = asset_acquire._unsafe_path("dest_path", _BytesPathLike())
    assert problem is not None
    assert "dest_path no es una ruta" in problem, problem
    assert "bytes" in problem, problem


def test_unsafe_path_rejects_control_characters(tmp_path: Path) -> None:
    """D7: los caracteres de control se rechazan igual que en R78."""
    assert asset_acquire._unsafe_path("stage_dir", "/tmp/a\x00b") is not None
    assert asset_acquire._unsafe_path("stage_dir", "/tmp/a\x1fb") is not None


def test_in_flight_under_lists_only_what_can_belong_to_someone_else(
    tmp_path: Path,
) -> None:
    """D7: `_in_flight_under` es quien decide si el árbol se puede borrar.

    Con el subárbol propio de D1, lo que aparece aquí es lo que **no** es de
    esta adquisición. Se comprueba cada forma: reciente sí, caducado no, pid
    propio sí, pid muerto no, y la forma que no crea `_move` nunca.
    """
    root = tmp_path / "stage"
    root.mkdir()
    fresh = root / "otro.bin.incomplete"
    fresh.write_bytes(b"parcial")
    stale = root / "muerto.bin.incomplete"
    stale.write_bytes(b"parcial")
    when = time.time() - asset_acquire.STALE_PARTIAL_AGE_S - 60
    os.utime(stale, (when, when))
    # Un temporal de instalación de **otro** destino, pero de este proceso: es
    # nuestro, así que está en vuelo.
    mine = root / f"raro.bin{asset_acquire.PARTIAL_MARK}{os.getpid()}-1"
    mine.write_bytes(b"parcial")
    # La forma exacta de `_move` con un pid que no existe ya.
    dead_pid = root / f"raro.bin{asset_acquire.PARTIAL_MARK}999999-1"
    dead_pid.write_bytes(b"parcial")
    # Un nombre que solo parece un temporal (R24).
    looks_like = root / "out.bin.partial-backup"
    looks_like.write_bytes(b"activo real")
    # El stageado de esta adquisición y su `.incomplete`: excluidos por nombre.
    # Su `<staged>.partial-<pid>-<seq>` también —es el temporal de instalación
    # de este mismo destino—, así que no aparece en la lista.
    staged = root / "out.bin"
    staged.write_bytes(b"parcial")
    (root / "out.bin.incomplete").write_bytes(b"parcial")
    (root / f"out.bin{asset_acquire.PARTIAL_MARK}{os.getpid()}-1").write_bytes(b"temp")

    found = asset_acquire._in_flight_under(root, staged)

    assert sorted(p.name for p in found) == [
        "otro.bin.incomplete",
        f"raro.bin{asset_acquire.PARTIAL_MARK}{os.getpid()}-1",
    ], sorted(p.name for p in found)


def test_cache_entries_for_lists_only_the_staged_files(tmp_path: Path) -> None:
    """D7: el barrido de la caché es por nombre, y es recursivo."""
    cache = tmp_path / ".cache"
    nested = cache / "huggingface" / "download" / "repo" / "model"
    nested.mkdir(parents=True)
    (cache / "out.bin.metadata").write_bytes(b"meta")
    (nested / "out.bin.incomplete").write_bytes(b"parcial")
    (nested / "otro.bin.incomplete").write_bytes(b"parcial ajeno")
    (nested / "out.binx.incomplete").write_bytes(b"parcial de otro nombre")

    entries = asset_acquire._cache_entries_for(cache, cache / "out.bin")

    assert sorted(p.name for p in entries) == ["out.bin.incomplete", "out.bin.metadata"]


def test_drop_client_cache_without_a_staged_path_keeps_a_foreign_download(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """D7: la rama `staged=None`, llamada directamente.

    No es alcanzable desde `acquire()` —los siete caminos terminales pasan el
    stageado—, pero existe y tiene que comportarse: sin a quién atribuir la
    pertenencia, un parcial ajeno en vuelo obliga a conservar el árbol entero en
    lugar de borrarlo a ciegas.
    """
    root = tmp_path / "stage"
    cache = root / ".cache"
    cache.mkdir(parents=True)
    (cache / "otro.bin.incomplete").write_bytes(b"descarga ajena en vuelo")

    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        asset_acquire._drop_client_cache(root)

    assert cache.is_dir(), "sin atribución posible no se borra nada"
    assert (cache / "otro.bin.incomplete").is_file()
    assert any("no se puede atribuir" in r.getMessage() for r in caplog.records), caplog.text


def test_drop_client_cache_without_a_staged_path_still_drops_an_idle_cache(
    tmp_path: Path,
) -> None:
    """D7 (regresión): la rama `staged=None` no se queda en «no borrar nunca»."""
    root = tmp_path / "stage"
    cache = root / ".cache"
    cache.mkdir(parents=True)
    (cache / "restos").write_bytes(b"basura")

    asset_acquire._drop_client_cache(root)

    assert not cache.exists(), "nadie en vuelo: el árbol se barre como siempre"


def test_a_declared_hash_governs_even_when_the_declared_size_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R84: con hash, el tamaño declarado que no cuadra no obliga a redescargar.

    El «skip» comparaba el tamaño medido contra `_declared_bytes` **antes** de
    mirar el hash, y sin proteger ese paso con la excepción que `_check_staged`
    ya aplicaba («con hash el contenido ya esta fijado»). Un manifiesto cuyo
    `bytes` no coincide con lo que HF sirve —otra revisión, un redondeo de LFS—
    hacía que un activo correcto y verificado se bajase en cada adquisición:
    justo lo que la política de reutilización sin redescarga prohíbe. Con hash
    declarado manda el hash; el tamaño solo guarda los activos publicados sin
    él.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    declared_wrong_size = 376637898
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=declared_wrong_size,
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    first = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    second = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )

    for label, report in (("primera", first), ("segunda", second)):
        entry = report.entries[0]
        assert entry.status == "skipped", f"{label}: {entry.error}"
        assert entry.bytes == len(FAKE_CONTENT), (
            f"{label}: el tamaño informado es el medido, no el declarado"
        )
    assert downloads.get("n", 0) == 0, (
        "un destino cuyo hash cuadra no se redescarga, aunque el manifiesto "
        "declare otro tamaño"
    )
    assert dest.read_bytes() == FAKE_CONTENT
    assert (dest.parent / "out.bin.integrity").is_file(), (
        "la verificación se cacheó: se llegó a la comprobación de integridad"
    )

    # Y si el hash **no** cuadra, el destino se repara: el tamaño declarado no
    # puede tapar un contenido que no es el del manifiesto. El cliente de
    # mentira escribe ahora el contenido que la nueva revisión declara.
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(b"otros pesos", downloads)
    )
    other = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=declared_wrong_size,
        remote_path="out.bin",
        sha256=_digest(b"otros pesos"),
    )
    third = asset_acquire.acquire(
        [other],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
    )
    assert third.entries[0].status == "downloaded", third.entries[0].error
    assert downloads["n"] == 1
    assert dest.read_bytes() == b"otros pesos"



def test_a_job_cancelled_when_the_staged_file_vanished_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R79: el cuarto camino donde el presupuesto ganaba a la cancelación.

    La comprobación previa al `rename` trataba `False` («no está») y `None` («no
    se pudo medir») igual, y en ambos casos devolvía `failed` sin consultar
    `should_cancel` cuando ya no quedaban intentos —con `max_attempts=1`, siempre—.
    Es el mismo síntoma de R73 en un camino distinto, y con la misma limpieza
    que los de cancelación, así que no había nada que lo delatara.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)

    def _vanish_after_verify(path: Path, need_: object) -> bool:
        path.unlink()  # la retención se lleva el stageado recién verificado
        return True

    monkeypatch.setattr(asset_acquire, "_verify_integrity", _vanish_after_verify)

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1  # False al entrar, True antes del rename

    events: list[tuple[str, str]] = []
    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
        should_cancel=_should_cancel,
        progress=lambda filename, state: events.append((filename, state)),
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.error is None, "cancelar no es un fallo"
    assert entry.attempts == 1
    assert slept == {}, "ni se quema el backoff que ya no sirve de nada"
    assert ("out.bin", "cancelled") in events
    assert report.failed == [], "un cancelado no cuenta como descarga fallida"
    assert not dest.exists()


def test_a_job_cancelled_when_the_staged_file_is_unmeasurable_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R79: el otro motivo de la misma comprobación, con el mismo derecho.

    Es el escenario de R71: un EIO midiendo el stageado justo antes del rename.
    También acababa en `failed` con `max_attempts=1`, porque el sondeo de
    `should_cancel` vivía después del presupuesto.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)
    _break_staged_presence_after_the_first(monkeypatch, stage, "out.bin")

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
        should_cancel=_should_cancel,
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert slept == {}
    assert report.failed == []
    assert downloads["n"] == 1
    assert not dest.exists()


def test_a_job_cancelled_on_a_guard_failure_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R79: las comprobaciones inmediatas tampoco pueden adelantarse.

    La guarda de espacio devolvía `failed` sin preguntar. Un job cancelado
    mientras se medía el volumen acababa en `report.failed`, que el agente lee
    como «la descarga no completó todos los archivos del grupo».
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)

    def _unreadable(paths: list[Path]) -> None:
        return None

    monkeypatch.setattr(asset_acquire, "_free_bytes_for", _unreadable)

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1

    report = asset_acquire.acquire(
        [need],
        env={},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
        should_cancel=_should_cancel,
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert slept == {}
    assert report.failed == []
    assert downloads.get("n", 0) == 0, "la guarda corta antes de descargar"


def test_a_job_cancelled_on_a_manifest_failure_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R79: un fallo inmediato del manifiesto tampoco es un fallo si se canceló.

    Mismo sondeo, mismo sitio: `_failure_entry` pregunta antes de construir la
    entrada, así que los cinco caminos inmediatos —origen ausente, `remote_path`
    inseguro, destino no absoluto, espacio ilegible y espacio insuficiente— no
    pueden dejar un cancelado en `report.failed`.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        repo_id=None,  # sin origen declarado
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    cancels = {"n": 0}

    def _should_cancel() -> bool:
        cancels["n"] += 1
        return cancels["n"] > 1

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
        should_cancel=_should_cancel,
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert entry.attempts == 0
    assert report.failed == []
    assert downloads.get("n", 0) == 0


# --- R80: el staging no puede coincidir con el destino -------------------------


def test_a_dot_remote_path_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R80: `remote_path='.'` se colaba porque `Path('.').parts` es vacío.

    `stage / '.'` **es** el directorio de staging, así que el stageado y el
    destino coincidían: el cliente escribía directo al nombre definitivo y el
    fallo terminal borraba el árbol de staging entero con `_remove_staged`.
    """
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path=".",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "remote_path inseguro" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"
    assert not dest.exists()
    assert stage.is_dir(), "el staging se queda como estaba"


def test_a_destination_inside_the_stage_dir_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R80: nada impide que un `dest_path` de manifiesto caiga en el staging.

    Si el destino está dentro del directorio de staging, `staged == dest`: el
    cliente escribe al nombre definitivo y `_move` degenera en
    `os.replace(dest, dest)`. Un parcial quedaría publicado bajo el nombre final,
    que es justo lo que el módulo existe para impedir.
    """
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    dest = stage / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "directorio de staging" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0
    assert not dest.exists()


def test_a_concurrent_staged_copy_survives_a_failed_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R80: el fallo de un `remote_path` ridículo no se cobra al vecino.

    Con `remote_path='.'` el stageado era el directorio de staging, y el camino
    de fallo definitivo llamaba a `_remove_staged(stage)` —que ve un directorio
    y hace `shutil.rmtree`—. La copia en vuelo de otro `acquire` concurrente
    sobre el mismo árbol desaparecía, y su `rename` fallaba sin que nadie supiera
    por qué.
    """
    stage = tmp_path / "stage"
    stage.mkdir(parents=True)
    inflight = stage / "otro.bin"
    inflight.write_bytes(b"copia en vuelo de otra adquisicion")
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path=".",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "remote_path inseguro" in (entry.error or ""), entry.error
    assert inflight.is_file(), "la copia del otro acquire sobrevive"
    assert inflight.read_bytes() == b"copia en vuelo de otra adquisicion"
    assert stage.is_dir(), "y el árbol de staging sigue en pie"


# --- R93: la limpieza de una adquisición no se cobra la de otra ---------------


def test_a_failed_acquire_keeps_another_acquires_in_flight_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R93: cancelar o fallar no borra la caché de otro `acquire` en vuelo.

    El staging es compartido (`default_stage_dir` es el mismo para todas), y
    `_drop_client_cache` hacía `rmtree` de todo `stage/.cache`: el `.incomplete`
    de la descarga en vuelo de otro job desaparecía y su `rename` fallaba sin
    que nadie supiera por qué. Una cancelación propia convertida en pérdida de
    datos ajena.

    D1: el escopado ya no se sostiene sobre el nombre. Cada adquisición
    descarga bajo su propio `job-<pid>-<seq>`, así que lo ajeno queda **fuera**
    del árbol que se recorre: aquí se siembra en el `.cache` heredado del
    staging compartido, que es donde una adquisición de antes —o un cliente que
    escriba ahí— puede haber dejado su descarga a medias.

    El estado del otro se siembra en disco en lugar de lanzar dos hilos de
    verdad: lo que se verifica es la **regla** —qué se borra cuando hay parciales
    ajenos bajo el staging—, y un hilo real introduciría una carrera sobre
    cuándo corre la limpieza sin añadir ninguna cobertura.
    """
    stage = tmp_path / "stage"
    downloads = stage / ".cache" / "huggingface" / "download" / "repo" / "model"
    downloads.mkdir(parents=True)
    foreign = downloads / "otro.bin.incomplete"
    foreign.write_bytes(b"descarga en vuelo de otra adquisicion")
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download_then_fail(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        token = Path(argv[argv.index("--local-dir") + 1])
        seen["token"] = token
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        # El cliente escribe sus `.incomplete` bajo el `.cache` de su
        # `--local-dir`, que desde D1 es el subárbol de esta adquisición.
        mine = token / ".cache" / "huggingface" / "download" / "repo" / "model"
        mine.mkdir(parents=True, exist_ok=True)
        (mine / "out.bin.incomplete").write_bytes(b"parcial")
        return 1  # el cliente falla: camino terminal

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert not list(stage.rglob("out.bin")), "lo propio sí se limpia"
    assert not list(seen["token"].rglob("*.incomplete")), "y su `.incomplete`"
    assert foreign.is_file(), "el del otro acquire sobrevive"
    assert foreign.read_bytes() == b"descarga en vuelo de otra adquisicion"
    assert (stage / ".cache").is_dir(), "el árbol compartido no se tira"
    assert not list(stage.glob("job-*")), "y el subárbol propio se retira vacío"


def test_the_whole_cache_is_still_dropped_when_nothing_else_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R93 (regresión): sin parciales ajenos, la limpieza es la de siempre.

    El escopado no puede quedarse en «no borrar nunca»: cuando este módulo es
    el único que escribe bajo el staging, el camino terminal sigue dejando
    `.cache` limpio para que una recuperación no encuentre restos.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download_then_fail(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        token = Path(argv[argv.index("--local-dir") + 1])
        seen["token"] = token
        target = token / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        cache = token / ".cache"
        nested = cache / "huggingface" / "download" / "repo" / "model"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "out.bin.incomplete").write_bytes(b"parcial")
        (cache / "x").write_bytes(b"cache")
        return 1

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert report.entries[0].status == "failed"
    assert not (seen["token"] / ".cache").exists(), "sin vecino en vuelo se borra todo"
    assert not list(stage.rglob("*"))


# --- D1: cada adquisición tiene su propio subárbol de staging ------------------


def test_a_same_name_foreign_download_survives_a_failed_acquire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1: la pertenencia es un prefijo de ruta, no un nombre.

    R93 dejaba un agujero que sus propias pruebas no veían: las dos sembraban
    un fichero ajeno con **otro** nombre. Con el mismo `remote_path`, el stageado
    del vecino se llamaba exactamente igual que el propio, así que
    `_is_staged_name` lo clasificaba como de esta adquisición y el camino
    terminal borraba su `.incomplete` y su caché: el trabajo de otro job
    desaparecía y su `rename` fallaba sin que nadie supiera por qué.

    Desde D1 cada `acquire()` descarga bajo `stage/job-<pid>-<seq>`, que es
    también su `--local-dir`. Lo ajeno es un hermano de ese subárbol, y la
    limpieza no lo recorre. Se siembra el estado del vecino en las dos formas
    que puede tener —su propio `job-*` y el layout heredado del staging
    compartido— y ninguna de las dos se toca.
    """
    stage = tmp_path / "stage"
    # El vecino con su propio subárbol, bajando el mismo `remote_path`.
    neighbour = stage / "job-999999-1"
    neighbour_cache = neighbour / ".cache" / "huggingface" / "download" / "repo" / "model"
    neighbour_cache.mkdir(parents=True)
    (neighbour / "out.bin").write_bytes(b"copia en vuelo de otra adquisicion")
    (neighbour_cache / "out.bin.incomplete").write_bytes(b"parcial ajeno")
    # Y el mismo estado en el layout heredado, directamente bajo el staging.
    legacy_cache = stage / ".cache" / "huggingface" / "download" / "repo" / "model"
    legacy_cache.mkdir(parents=True)
    (stage / "out.bin").write_bytes(b"copia en vuelo de otra adquisicion")
    (legacy_cache / "out.bin.incomplete").write_bytes(b"parcial ajeno")
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download_then_fail(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        seen["token"] = Path(argv[argv.index("--local-dir") + 1])
        target = seen["token"] / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        return 1  # el cliente falla: camino terminal

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_fail)
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert report.entries[0].status == "failed", report.entries[0].error
    assert (neighbour / "out.bin").is_file(), "el stageado del vecino sobrevive"
    assert (neighbour_cache / "out.bin.incomplete").is_file(), "y su `.incomplete`"
    assert (stage / "out.bin").is_file(), "el layout heredado tampoco se toca"
    assert (legacy_cache / "out.bin.incomplete").is_file()
    assert not seen["token"].exists(), "el subárbol propio se retira"


def test_a_successful_install_reaps_its_own_staging_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 (coste a): el subárbol vacío se retira; el informe sigue igual.

    El `rename` consume el stageado, así que el `job-<pid>-<seq>` de esta
    adquisición quedaría vacío para siempre. Se retira —es basura de este
    proceso y borrarlo no puede tocar al vecino— y `report.stage_dir` sigue
    publicando el staging **compartido**, para no cambiarle el significado a
    quien ya consume el informe.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        seen["token"] = Path(argv[argv.index("--local-dir") + 1])
        return _fake_download(FAKE_CONTENT)(argv, timeout=timeout, should_cancel=should_cancel)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download)

    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    assert report.entries[0].status == "downloaded", report.entries[0].error
    assert report.stage_dir == str(stage), "el informe publica el staging compartido"
    assert not seen["token"].exists(), "el subárbol propio se retira vacío"
    assert not list(stage.rglob("*")), "y el staging queda limpio"


def test_a_surviving_client_cache_keeps_its_subtree_but_not_the_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 (coste a, segundo plano): lo no vacío no se tira.

    La `.cache` del cliente se conserva en el camino de éxito, igual que se
    conservaba bajo el staging compartido. Con el subárbol propio eso significa
    que el directorio sobrevive —no está vacío—, y quien lo retira es la
    retención (`cache_policy`), no la adquisición.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, Path] = {}

    def _download(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        token = Path(argv[argv.index("--local-dir") + 1])
        seen["token"] = token
        (token / ".cache" / "x").parent.mkdir(parents=True, exist_ok=True)
        (token / ".cache" / "x").write_bytes(b"cache")
        return _fake_download(FAKE_CONTENT)(argv, timeout=timeout, should_cancel=should_cancel)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download)

    report = asset_acquire.acquire(
        [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    assert report.entries[0].status == "downloaded", report.entries[0].error
    assert (seen["token"] / ".cache" / "x").is_file(), "la caché del cliente se conserva"
    assert dest.read_bytes() == FAKE_CONTENT


def test_a_live_neighbours_install_temp_is_reaped_once_it_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D1 (riesgo conocido): el directorio de destino sigue siendo compartido.

    El subárbol de staging cerró la misma-nombre del lado de la descarga, pero
    la instalación escribe su temporal en el directorio del destino y ahí la
    pertenencia solo se puede preguntar por pid y por edad. Esta prueba fija el
    comportamiento actual —**lo documenta, no lo bendice**— para que un refactor
    futuro tenga un objetivo: el temporal de un vecino vivo que lleva inactivo
    más de `STALE_PARTIAL_AGE_S` se barre, y el recién empezado se respeta.

    La consecuencia está acotada y por eso se puede vivir con ella: el vecino
    ve fallar su `rename`, reintenta y baja el activo otra vez. Ningún parcial
    se publica, porque el barrido solo roza los `<dest>.partial-*` y el destino
    no entra en él.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    # Un vecino de verdad: proceso vivo que este test puede señalar.
    neighbour = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stale = dest.parent / f"out.bin.partial-{neighbour.pid}-3"
        stale.write_bytes(b"copia cross-device en vuelo")
        fresh = dest.parent / f"out.bin.partial-{neighbour.pid}-4"
        fresh.write_bytes(b"copia cross-device recien empezada")
        for path, age_s in (
            (stale, asset_acquire.STALE_PARTIAL_AGE_S + 60),
            (fresh, 5),
        ):
            when = time.time() - age_s
            os.utime(path, (when, when))

        need = _acquire_need(
            filename="out.bin",
            dest_path=str(dest),
            bytes=len(FAKE_CONTENT),
            remote_path="out.bin",
            sha256=_digest(),
        )
        monkeypatch.setattr(
            asset_acquire, "_run_hf_download", _fake_download(FAKE_CONTENT)
        )

        report = asset_acquire.acquire(
            [need], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
        )

        assert report.entries[0].status == "downloaded", report.entries[0].error
        assert not stale.exists(), (
            "el temporal inactivo de un vecino vivo se barre: coste conocido de D1"
        )
        assert fresh.is_file(), "el recién empezado se respeta"
    finally:
        neighbour.terminate()
        neighbour.wait()


# --- R81: preparar el directorio del destino no es instalar --------------------


def test_an_unmeasurable_destination_directory_is_not_an_install_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R81: un fallo midiendo el padre no se atribuye al `rename`.

    Con `exist_ok=True`, un `os.mkdir` que falla lleva a `Path.mkdir` a medir el
    padre con `self.is_dir()`, y ese `stat` se re-lanzaba dentro del `try` de la
    instalación: el informe decía «no se pudo instalar» de un rename que nunca se
    intentó, se gastaba el backoff y el fallo definitivo borraba la descarga
    verificada. Además, `_dest_size` daría `None` para siempre, así que el activo
    se volvería a bajar en cada adquisición.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    slept = _no_sleep(monkeypatch)

    real_mkdir = Path.mkdir

    def _eio(self, *args, **kwargs):  # noqa: ANN001
        if self == dest.parent:
            raise OSError(errno.EIO, "Input/output error")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _eio)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo preparar el directorio del destino" in (entry.error or ""), (
        entry.error
    )
    assert "no se pudo instalar" not in (entry.error or ""), (
        "el rename no se intentó: no se le puede atribuir el fallo"
    )
    assert entry.attempts == 1, "no se reintenta lo que no se puede arreglar"
    assert slept == {}, "ni se quema el backoff"
    assert downloads["n"] == 1
    assert _staged(stage, "out.bin").is_file(), (
        "la descarga verificada no se tira por no poder crear un directorio"
    )
    assert not dest.exists()


# --- R87: cancelar manda también en los dos caminos que quedaban -------------


def _cancel_after_the_first_poll() -> tuple[Callable[[], bool], dict[str, int]]:
    """`should_cancel` que solo cancela a partir de la segunda consulta.

    La primera consulta la hace `acquire()` antes de entrar en la necesidad: si
    responde `true` ahí, la necesidad ni se intenta y se reporta «no intentado».
    Para probar los caminos **terminales** de `_acquire_one` hace falta llegar a
    ellos, así que el cancelado cae justo después.
    """
    polls = {"n": 0}

    def _should_cancel() -> bool:
        polls["n"] += 1
        return polls["n"] > 1

    return _should_cancel, polls


def test_a_job_cancelled_preparing_the_destination_directory_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R87: el `mkdir` del destino fallido de un job cancelado no es `failed`.

    El camino de R81 construía el `AcquiredEntry` a mano, saltándose
    `_failure_entry` —el sitio que convierte un fallo en cancelación (R79)—. Un
    job cancelado con un EIO preparando el directorio acababa en `report.failed`
    con `cancelled=False`: el agente lo lee como «la descarga no completó todos
    los archivos del grupo» en lugar de como cancelación.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    should_cancel, polls = _cancel_after_the_first_poll()

    real_mkdir = Path.mkdir

    def _eio(self, *args, **kwargs):  # noqa: ANN001
        if self == dest.parent:
            raise OSError(errno.EIO, "Input/output error")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _eio)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
        should_cancel=should_cancel,
    )

    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert report.cancelled is True
    assert report.failed == [], "un cancelado no es un fallo del grupo"
    assert entry.attempts == 1
    assert downloads["n"] == 1, "la descarga se pagó antes de preparar el directorio"
    assert polls["n"] >= 2, "la cancelación se consultó en el camino terminal"


def test_a_job_cancelled_with_no_attempts_left_is_not_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R87: `max_attempts<=0` con cancelación tampoco acaba en `failed`.

    La cola del bucle —que solo se alcanza con `max_attempts<=0`— construía
    también su entrada a mano. Un job cancelado reportaba `failed` con
    `report.cancelled=False`, el mismo residuo de R79 que el camino del `mkdir`.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    should_cancel, polls = _cancel_after_the_first_poll()

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=0,
        should_cancel=should_cancel,
    )

    entry = report.entries[0]
    assert entry.status == "cancelled", entry.error
    assert report.cancelled is True
    assert report.failed == [], "un cancelado no es un fallo del grupo"
    assert entry.attempts == 0, "sin intentos no se reintenta nada"
    assert downloads.get("n", 0) == 0, "ni se llega a descargar"
    assert polls["n"] >= 2

    # Y sin cancelación sigue siendo el fallo informativo de siempre.
    plain = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=0,
    )
    assert plain.entries[0].status == "failed"
    assert "max_attempts<=0" in (plain.entries[0].error or "")
    assert plain.cancelled is False


# --- R82: el contenido se sincroniza antes de publicar el nombre ---------------


def test_the_same_filesystem_rename_fsyncs_the_content_before_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R82: en el mismo filesystem, los bytes se fuerzan **antes** del rename.

    `os.replace` garantiza que el nombre aparezca de una pieza; no garantiza que
    lo que nombra esté en disco. Los bytes los escribió el cliente `hf` y este
    módulo no los había forzado —el camino EXDEV sí, vía `_copy_and_fsync`—, así
    que un corte de luz justo después podía publicar el nombre nuevo con
    contenido incompleto. Sincronizar solo el directorio no lo evita.

    El orden es lo que se fija aquí: el `fsync` del stageado tiene que ocurrir
    antes del `os.replace`, porque después el fichero ya se llama `dest`.
    """
    src = tmp_path / "stage" / "out.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"contenido" * 1000)
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)

    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace
    src_id = src.stat().st_ino

    def _fsync(fd: int) -> None:
        try:
            stat = os.fstat(fd)
        except OSError:
            stat = None
        events.append("fsync:staged" if stat and stat.st_ino == src_id else "fsync:dir")
        return real_fsync(fd)

    def _replace(a, b):  # noqa: ANN001
        events.append("replace")
        return real_replace(a, b)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)

    asset_acquire._move(src, dest)

    assert "fsync:staged" in events, events
    assert events.index("fsync:staged") < events.index("replace"), (
        f"el contenido se sincroniza antes de publicar el nombre: {events}"
    )
    assert events[-1] == "fsync:dir", "y la entrada del directorio después"
    assert dest.read_bytes() == b"contenido" * 1000
    assert not src.exists(), "el stageado se consume al instalar"


def test_an_unsupported_fsync_on_the_staged_file_does_not_break_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R82: hay filesystems que no aceptan `fsync` sobre un `O_RDONLY`.

    No es razón para perder una instalación que ya está publicada: se anota a
    `debug` y se sigue, igual que `_fsync_dir`.
    """
    src = tmp_path / "stage" / "out.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"contenido")
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)

    real_fsync = os.fsync
    calls = {"n": 0}

    def _eopnotsupp(fd: int) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _eopnotsupp)

    asset_acquire._move(src, dest)

    assert dest.read_bytes() == b"contenido"
    assert calls["n"] >= 2, "el fsync del directorio se intenta igual"


# --- R78: los datos del manifiesto no pueden abortar el plan -------------------


def test_manifest_fields_with_control_characters_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: un NUL en un campo del manifiesto es una entrada, no una excepción.

    `pathlib` y `subprocess` responden a un NUL con `ValueError`, no con
    `OSError`. Todos los límites del módulo capturaban `OSError`, así que un
    `dest_path` con NUL salía de `acquire()` **después** de haber pagado la
    descarga —con el stageado huérfano, sin informe y con las necesidades
    siguientes sin intentar—, y un `repo_id` con NUL lo hacía antes, por
    `subprocess.Popen`. Se rechazan donde ya se validaba `remote_path`.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    fields = (
        ("dest_path", "dest_path", str(dest) + "\x00.bin"),
        ("remote_path", "remote_path", "out.bin\x00"),
        ("repo_id", "repo_id", "repo/model\x00"),
        ("revision", "revision", "main\x00"),
        # R89: `repo_type` acaba en la línea de comandos (`--repo-type`), así
        # que es un campo de frontera más.
        ("repo_type", "repo_type", "mo\x01del"),
    )

    for label, attribute, poisoned in fields:
        need = _acquire_need(
            filename="out.bin",
            dest_path=poisoned if attribute == "dest_path" else str(dest),
            bytes=len(FAKE_CONTENT),
            remote_path=poisoned if attribute == "remote_path" else "out.bin",
            repo_id=poisoned if attribute == "repo_id" else "repo/model",
            revision=poisoned if attribute == "revision" else None,
            repo_type=poisoned if attribute == "repo_type" else "model",
            sha256=_digest(),
        )
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            max_attempts=1,
        )
        entry = report.entries[0]
        assert entry.status == "failed", f"{label}: hay informe, no una excepción"
        assert label in (entry.error or ""), (label, entry.error)
        assert "caracteres de control" in (entry.error or ""), entry.error
        assert downloads.get("n", 0) == 0, f"{label}: no se lanza el cliente"

    assert report.failed, "el informe dice qué campo está mal"
    assert not (tmp_path / "models" / "out.bin").exists()


def test_an_hf_cli_with_control_characters_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: el `hf_cli` también viene de fuera (`HF_CLI` lo escribe un operador).

    Un NUL ahí hace que `subprocess.Popen` responda `ValueError`, que ningún
    `except OSError` ve. Se rechaza antes del bucle, con una entrada por
    necesidad —como cuando no hay cliente—, para que el llamador sepa de todos
    los activos.
    """
    stage = tmp_path / "stage"
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    needs = [
        _acquire_need(
            filename=f"m{i}.bin",
            dest_path=str(tmp_path / f"m{i}.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path=f"m{i}.bin",
            sha256=_digest(),
        )
        for i in range(2)
    ]

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf\x00",
        stage_dir=stage,
    )

    assert len(report.entries) == 2, "ninguna necesidad desaparece del informe"
    for entry in report.entries:
        assert entry.status == "failed", entry.error
        assert "HF_CLI" in (entry.error or ""), entry.error
        assert "caracteres de control" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0
    assert not (tmp_path / "m0.bin").exists()


def test_undeclared_bytes_that_are_not_a_number_become_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: `int(need.bytes or 0)` sobre una entrada mal formada no aborta.

    `"bytes": "muchos"` en el manifiesto lanzaba `ValueError` desde el «skip» y
    desde `_check_staged`, y salía de `acquire()` sin informe. Un tamaño
    ilegible es «no declarado»: sin tamaño manda el hash, y sin hash no se
    comprueba nada más.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=0,
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.bytes = "muchos"  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert downloads["n"] == 1
    assert dest.read_bytes() == FAKE_CONTENT


def test_unreadable_environment_numbers_fall_back_to_the_documented_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: un `HF_FETCH_TIMEOUT`/`HF_DISK_MARGIN_BYTES` ilegible no aborta.

    `int(float(...))` sobre el entorno lanzaba `ValueError` antes de tocar nada:
    sin informe y sin descargas, por una variable que escribe un operador. Se
    sustituye por el valor documentado, y se dice en el log.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    seen: dict[str, int] = {}

    def _record(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        seen["timeout"] = timeout
        return _fake_download(FAKE_CONTENT)(
            argv, timeout=timeout, should_cancel=should_cancel
        )

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _record)

    # Sitio de sobra: la descarga se lanza, y con ella el timeout por defecto.
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: 10**12)
    report = asset_acquire.acquire(
        [need],
        env={"HF_FETCH_TIMEOUT": "abc", "HF_DISK_MARGIN_BYTES": "1e"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert seen["timeout"] == asset_acquire.DEFAULT_HF_FETCH_TIMEOUT_S, (
        "el timeout ilegible cae en el valor documentado"
    )

    # Y el margen ilegible también: la guarda falla pidiendo el margen por
    # defecto, no uno inventado.
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: 1)
    again = asset_acquire.acquire(
        [_acquire_need(
            filename="otro.bin",
            dest_path=str(tmp_path / "models" / "otro.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="otro.bin",
            sha256=_digest(),
        )],
        env={"HF_FETCH_TIMEOUT": "abc", "HF_DISK_MARGIN_BYTES": "1e"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    failed = again.entries[0]
    assert failed.status == "failed", "hay informe, no una excepción"
    assert f"{asset_acquire.DEFAULT_DISK_MARGIN_BYTES / asset_acquire.GiB:.1f} GiB" in (
        failed.error or ""
    ), f"el margen ilegible también: {failed.error}"


def test_infinite_numbers_fall_back_instead_of_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R85: `inf`/`-inf`/`1e400` son `OverflowError`, no `ValueError`.

    Las sondas de R78 solo cubrían `abc`/`1e`/`muchos`, que `int()` responde con
    `ValueError`. Un infinito responde con `OverflowError` —un `ArithmeticError`
    que ninguno de los dos conversores veía—, así que `HF_FETCH_TIMEOUT=inf`,
    `HF_DISK_MARGIN_BYTES=1e400` o un `"bytes": Infinity` del manifiesto
    (`json.load` acepta el literal no estándar) abortaban `acquire()` sin una
    sola entrada de informe y con las necesidades siguientes sin intentar. Un
    número no finito es ilegible: cae en el valor documentado, o en «no
    declarado», y se dice en el log.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    seen: dict[str, int] = {}

    def _record(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        seen["timeout"] = timeout
        return _fake_download(FAKE_CONTENT)(
            argv, timeout=timeout, should_cancel=should_cancel
        )

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _record)
    _no_sleep(monkeypatch)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    # Sitio de sobra: lo que se comprueba es el timeout por defecto, no la guarda.
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: 10**12)
    for poisoned in ("inf", "-inf", "1e400"):
        report = asset_acquire.acquire(
            [need],
            env={"HF_FETCH_TIMEOUT": poisoned, "HF_DISK_MARGIN_BYTES": "1e400"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            max_attempts=1,
        )
        entry = report.entries[0]
        assert entry.status == "downloaded", f"{poisoned}: {entry.error}"
        assert seen["timeout"] == asset_acquire.DEFAULT_HF_FETCH_TIMEOUT_S, (
            f"{poisoned}: el timeout infinito cae en el valor documentado"
        )
        assert dest.read_bytes() == FAKE_CONTENT
        dest.unlink()

    # Y el margen infinito también cae en el documentado: la guarda falla
    # pidiéndolo, no con un `OverflowError` sin informe.
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: 1)
    report = asset_acquire.acquire(
        [_acquire_need(
            filename="margen.bin",
            dest_path=str(tmp_path / "models" / "margen.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="margen.bin",
            sha256=_digest(),
        )],
        env={"HF_DISK_MARGIN_BYTES": "1e400"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )
    failed = report.entries[0]
    assert failed.status == "failed", "hay informe, no una excepción"
    assert f"{asset_acquire.DEFAULT_DISK_MARGIN_BYTES / asset_acquire.GiB:.1f} GiB" in (
        failed.error or ""
    ), f"el margen infinito cae en el documentado: {failed.error}"

    # Y un `bytes` no finito del manifiesto es «no declarado»: sin tamaño manda
    # el hash, y el activo se adquiere y se reutiliza con normalidad.
    infinite = _acquire_need(
        filename="infinito.bin",
        dest_path=str(tmp_path / "models" / "infinito.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="infinito.bin",
        sha256=_digest(),
    )
    infinite.bytes = float("inf")  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: 10**12)

    first = asset_acquire.acquire(
        [infinite], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )
    second = asset_acquire.acquire(
        [infinite], env={"HF_DISK_GUARD": "0"}, hf_cli="/fake/hf", stage_dir=stage
    )

    assert first.entries[0].status == "downloaded", first.entries[0].error
    assert first.entries[0].bytes == len(FAKE_CONTENT), (
        "el tamaño se mide, no se lee del manifiesto infinito"
    )
    assert second.entries[0].status == "skipped", second.entries[0].error
    assert downloads["n"] == 1, "y no se redescarga en la segunda pasada"
    assert (tmp_path / "models" / "infinito.bin").read_bytes() == FAKE_CONTENT


def test_a_value_error_from_the_download_boundary_becomes_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: lo que `subprocess.Popen` responde con `ValueError` también.

    La validación de control characters rechaza los campos que este módulo
    conoce, pero el límite se ensancha igual: un `ValueError` que llegue desde
    cualquier punto de la frontera —`Popen`, `Path.mkdir`, `os.replace`, `open`—
    tiene que ser una entrada, no una excepción sin informe.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )

    def _null_byte(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        raise ValueError("embedded null byte")

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _null_byte)
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo ejecutar" in (entry.error or ""), entry.error
    assert "embedded null byte" in (entry.error or ""), entry.error
    assert entry.attempts == 1
    assert not dest.exists()


def test_a_value_error_installing_becomes_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: un `ValueError` del `rename` es un fallo de instalación.

    Es el caso que salía **después** de pagar la descarga: `os.replace` con un
    NUL en el destino responde `ValueError: replace: embedded null character in
    dst`, que el `except OSError` de la instalación no veía.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    def _null_byte(src: Path, target: Path) -> None:
        raise ValueError("replace: embedded null character in dst")

    monkeypatch.setattr(asset_acquire, "_move", _null_byte)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=2,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo instalar en" in (entry.error or ""), entry.error
    assert "embedded null character" in (entry.error or ""), entry.error
    assert entry.attempts == 2, "se reintenta el rename como cualquier transitorio"
    assert downloads["n"] == 1, "sin volver a descargar"


def test_a_value_error_preparing_the_destination_becomes_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R78: `Path.mkdir` también responde `ValueError`, y tiene su propio camino.

    Desde R81 la preparación del directorio del destino vive en su propio
    `try`; que ese `try` capture solo `OSError` dejaría fuera el NUL, que es
    exactamente lo que este módulo dice no poder permitirse.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    real_mkdir = Path.mkdir

    def _null_byte(self, *args, **kwargs):  # noqa: ANN001
        if self == dest.parent:
            raise ValueError("mkdir: embedded null byte")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _null_byte)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=3,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "no se pudo preparar el directorio del destino" in (entry.error or ""), (
        entry.error
    )
    assert "embedded null byte" in (entry.error or ""), entry.error
    assert entry.attempts == 1
    assert _staged(stage, "out.bin").is_file(), "la descarga verificada no se tira"


# --- R83: la guarda de disco en su camino por defecto --------------------------


def test_the_disk_guard_fails_closed_when_it_is_not_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R83: la guarda se prueba **sin** desactivarla.

    Todas las pruebas del módulo pasaban `HF_DISK_GUARD=0`, así que el contrato
    de fallar cerrado —documentado en el módulo desde esta ronda— no se ejercía
    en su camino por defecto. Un volumen demasiado pequeño corta antes de
    descargar, y lo dice.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        # Sin HF_DISK_GUARD: el valor por defecto es tenerla puesta.
        env={},
        hf_cli="/fake/hf",
        stage_dir=stage,
        # Más margen del que cabe en cualquier volumen de prueba.
        margin_bytes=10**15,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", "falla cerrado, no descarga a ver qué pasa"
    assert "espacio insuficiente" in (entry.error or ""), entry.error
    assert entry.attempts == 1
    assert downloads.get("n", 0) == 0, "la guarda corta antes de gastar ancho de banda"
    assert not dest.exists()


def test_the_disk_guard_fails_closed_when_the_volume_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R83: un volumen ilegible también corta, y no se descarga «a ver qué pasa»."""
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    monkeypatch.setattr(asset_acquire, "_free_bytes_for", lambda paths: None)

    report = asset_acquire.acquire(
        [need],
        env={},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "espacio libre ilegible" in (entry.error or ""), entry.error
    assert downloads.get("n", 0) == 0
    assert not dest.exists()


def test_an_install_only_retry_does_not_walk_into_enospc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R83: el destino se mide antes de la copia cross-device.

    La guarda completa corre antes de descargar y no se reevalúa en un reintento
    de instalación a propósito (R36), pero el camino EXDEV **consume** espacio en
    el destino. Ahí el ENOSPC se pagaba copiando GBs para fallar en el `rename`
    final, quemando los intentos y con ellos la descarga ya verificada. Un
    `statvfs` del destino antes de escribir lo dice sin copiar nada.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    def _exdev(a, b):  # noqa: ANN001
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "replace", _exdev)
    copies: dict[str, int] = {}

    def _record_copy(src: Path, dst: Path) -> None:
        copies["n"] = copies.get("n", 0) + 1

    monkeypatch.setattr(asset_acquire, "_copy_and_fsync", _record_copy)
    # El destino no tiene sitio: ni la guarda previa (staging y destino miden
    # igual) ni la copia pueden pasar.
    monkeypatch.setattr(asset_acquire, "_free_bytes", lambda path: 1)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed"
    assert "espacio insuficiente en el destino" in (entry.error or ""), entry.error
    assert copies.get("n", 0) == 0, "no se copia ni un byte a un volumen lleno"
    assert downloads["n"] == 1, "la descarga sí se pagó: es la guarda previa la que manda"
    assert not dest.exists()


# --- R96: la marca de «instalado» no basta cuando el manifiesto fija hash -----


def test_a_marker_from_a_hashless_revision_does_not_authorise_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R96: una marca con `digest: null` no autoriza un activo cuyo hash ya se fija.

    La marca se escribe con el hash que declara el manifiesto **en ese momento**
    (`"digest": null` bajo una revisión sin hash). Si el manifiesto pasa a fijar
    un sha256, el `recorded in (None, expected)` de entonces daba la marca por
    buena: el activo se reutilizaba sin verificar nada, que es justo lo que la
    marca de «instalado» no puede hacer cuando ya hay un hash que comprobar.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    marker = dest.parent / "out.bin.installed"
    marker.write_text(
        json.dumps({"installed": True, "bytes": len(FAKE_CONTENT), "digest": None}),
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    # El destino no se deja medir: es el caso para el que la marca existe, y el
    # único en el que su `digest` es la prueba.
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "la marca sin el hash declarado no reutiliza"
    # El destino sigue sin medirse, así que la instalación vuelve a escribir la
    # marca: ahora con el hash que el manifiesto declara, no con el `null` de la
    # revisión anterior.
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["digest"] == _digest(), recorded


def test_a_marker_with_the_declared_digest_still_authorises_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R96 (regresión): la marca que **sí** registra el hash sigue valiendo.

    El endurecimiento no puede llevarse por delante el caso para el que la
    marca existe: un destino que no se deja medir, instalado y verificado por
    este módulo con el hash que el manifiesto declara.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    marker = dest.parent / "out.bin.installed"
    marker.write_text(
        json.dumps(
            {"installed": True, "bytes": len(FAKE_CONTENT), "digest": _digest()}
        ),
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert downloads.get("n", 0) == 0, "con el hash registrado se reutiliza"
    assert marker.is_file(), "aquí la marca sí es la prueba"


# --- D3: la marca de «instalado» también está atada al tamaño declarado --------


def test_a_bytes_revision_bump_is_not_reused_from_the_install_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: sin hash, la marca tiene que registrar el tamaño **declarado ahora**.

    El lado sin hash de `_inspect_dest` se autorizaba con `bool(declared)`: con
    que el manifiesto declarara *algún* tamaño, la marca daba por bueno el
    destino sin comparar el suyo. Una revisión que solo cambia `bytes` —mismo
    nombre de fichero, sin hash— reutilizaba por tanto el activo anterior para
    siempre: el destino no se medía (es el caso de la marca), nadie volvía a
    bajar y el activo en disco no era el que el manifiesto pedía. La marca
    compara su `bytes` con el declarado, igual que `_check_staged` compara el
    medido.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 12)
    marker = dest.parent / "out.bin.installed"
    marker.write_text(
        json.dumps({"installed": True, "bytes": 12, "digest": None}),
        encoding="utf-8",
    )
    # Revisión 1 del manifiesto: 12 bytes, sin hash. La marca cuadra.
    rev1 = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=12,
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    sizes = {"bytes": 12}

    def _download(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        target = Path(argv[argv.index("--local-dir") + 1]) / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * sizes["bytes"])
        downloads["n"] = downloads.get("n", 0) + 1
        return 0

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download)
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    first = asset_acquire.acquire(
        [rev1],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert first.entries[0].status == "skipped", first.entries[0].error
    assert first.entries[0].bytes == 12, "el tamaño es el que la marca registró"
    assert downloads.get("n", 0) == 0, "la revisión que cuadra no redescarga"

    # Revisión 2: mismo nombre de fichero, sin hash, 96 bytes. La marca de la
    # revisión anterior ya no describe al activo que el manifiesto pide.
    sizes["bytes"] = 96
    rev2 = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=96,
        remote_path="out.bin",
    )

    second = asset_acquire.acquire(
        [rev2],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert second.entries[0].status == "downloaded", second.entries[0].error
    assert downloads["n"] == 1, "un bytes que no cuadra obliga a bajar el activo"
    assert dest.read_bytes() == b"x" * 96


def test_an_install_marker_with_an_unusable_size_reports_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D3: el `bytes` de la marca no se hace eco sin comprobar que es un tamaño.

    El destino no se deja medir —es el caso para el que la marca existe—, así
    que su `bytes` es un número que este módulo escribió en su día y que nadie
    ha vuelto a mirar. Se publica solo si es un entero positivo; si es `null`,
    una cadena o un negativo, el informe dice `0` —desconocido— en lugar de
    repetir un número que no describe el activo que hay en disco.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    (dest.parent / "out.bin.installed").write_text(
        json.dumps({"installed": True, "bytes": "99999", "digest": _digest()}),
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert downloads.get("n", 0) == 0, "el hash registrado sí autoriza la reutilización"
    assert entry.bytes == 0, "un tamaño ilegible se informa como desconocido"


def test_an_install_marker_without_its_file_does_not_skip_the_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R77: la marca sin fichero detrás es basura, no un activo instalado.

    La marca existe para un destino que no se deja medir, y `_dest_size`
    responde `None` tanto para «no se pudo medir» como para «no hay nada». Sin
    distinguirlos, un `<dest>.installed` huérfano —una instalación que alguien
    borró— saltaría un activo que no está en disco.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    (dest.parent / "out.bin.installed").write_text(
        '{"bytes": 5, "digest": "lo-que-sea", "installed": true}',
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "sin fichero no hay nada que reutilizar"
    assert dest.read_bytes() == FAKE_CONTENT


# --- R86/R90: la marca de «instalado» no puede ser la única prueba ------------


def _break_dest_exists_forever(
    monkeypatch: pytest.MonkeyPatch, dest: Path, error: OSError
) -> None:
    """`Path.exists()` del destino falla siempre: el disco no responde.

    Es la situación que la marca de «instalado» (R77) existe para cubrir, y
    también la que permite abusar de ella: con `exists()` roto, «no se pudo
    medir» y «ya no está» devuelven lo mismo.
    """
    real_exists = Path.exists

    def _exists(self, *args, **kwargs):  # noqa: ANN001
        if self == dest:
            raise error
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", _exists)


def test_an_installed_marker_alone_does_not_report_an_absent_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R86: marca huérfana + disco que no responde => se redescarga.

    `_destination_looks_present` respondía «sí está» cuando `exists()` fallaba,
    que es justo el caso FUSE/NFS/SMB para el que la marca existe. Con la marca
    como única prueba, un destino **borrado** cuyo `stat()` sigue dando EIO se
    reportaba `skipped` con `bytes` del manifiesto: un activo ausente presentado
    como presente, y una marca que nadie retiraba. Ahora, si el disco no deja
    confirmar ni el nodo ni su entrada de directorio, se falla cerrado.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    # La marca cuadra con lo que declara el manifiesto: solo le falta el activo.
    (dest.parent / "out.bin.installed").write_text(
        json.dumps({"installed": True, "bytes": 5, "digest": _digest()}),
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_exists_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads["n"] == 1, "sin activo en disco no hay nada que reutilizar"
    assert dest.read_bytes() == FAKE_CONTENT
    assert entry.bytes == len(FAKE_CONTENT), (
        "el tamaño es el medido tras instalar, no el de la marca caduca"
    )


def test_a_measurable_destination_that_is_skipped_leaves_no_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R90: la marca de «instalado» se retira también al reutilizar.

    El borrado de la marca vivía solo en el camino de instalación, así que un
    destino correcto y medible que se saltaba la dejaba en disco para siempre.
    Una marca que sobrevive a su destino ya no describe nada, y cuando el disco
    empieza a fallar se convierte en la única prueba de un activo que no está
    (R86). Quien reutiliza un destino **medido** la retira.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    marker = dest.parent / "out.bin.installed"
    marker.write_text(
        json.dumps({"installed": True, "bytes": 5, "digest": _digest()}),
        encoding="utf-8",
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert entry.bytes == len(FAKE_CONTENT)
    assert downloads.get("n", 0) == 0
    assert not marker.exists(), "la marca de un destino medible no sobrevive"
    assert (dest.parent / "out.bin.integrity").is_file(), (
        "lo que fija la reutilización es la caché de integridad, no la marca"
    )

    # Y un destino que **no** se deja medir sigue conservándola: es lo único que
    # evita bajarlo en cada pasada (R77).
    unmeasurable = tmp_path / "models" / "raro.bin"
    unmeasurable.parent.mkdir(parents=True, exist_ok=True)
    unmeasurable.write_bytes(FAKE_CONTENT)
    (unmeasurable.parent / "raro.bin.installed").write_text(
        json.dumps(
            {"installed": True, "bytes": len(FAKE_CONTENT), "digest": _digest()}
        ),
        encoding="utf-8",
    )
    _break_dest_stat_forever(
        monkeypatch, unmeasurable, OSError(errno.EIO, "Input/output error")
    )
    again = asset_acquire.acquire(
        [_acquire_need(
            filename="raro.bin",
            dest_path=str(unmeasurable),
            bytes=len(FAKE_CONTENT),
            remote_path="raro.bin",
            sha256=_digest(),
        )],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert again.entries[0].status == "skipped", again.entries[0].error
    assert downloads.get("n", 0) == 0, "el destino no medible se sigue reutilizando"
    assert (unmeasurable.parent / "raro.bin.installed").is_file(), (
        "aquí la marca sí es la prueba: no hay otra"
    )


# --- Cierre R1-R6 ------------------------------------------------------------


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "1e400"])
def test_a_non_finite_meta_default_bytes_does_not_kill_the_storage_script(
    tmp_path: Path, literal: str
) -> None:
    """R1: `meta.default_bytes` es dato de manifiesto, igual que `models[].bytes`.

    D4 blindó `_entry_bytes` (`scripts/storage_requirement.py:144`) y dejó el
    `int()` de `meta.default_bytes` pelado: un manifiesto de operario con
    `"default_bytes": Infinity` sacaba el script con rc=1 y una traza de
    `OverflowError` —un `ArithmeticError`, que no es ni `TypeError` ni
    `ValueError`—, mientras `check_models_manifest.py` informaba del mismo
    manifiesto como simplemente inválido. Dos lectores, dos respuestas. La regla
    es la de `capabilities._declared_bytes`: un tamaño ilegible es «no
    declarado», cuenta 0, y el requisito se publica.
    """
    raw = json.dumps({"meta": {"default_bytes": "__BYTES__"}, "models": []}).replace(
        '"__BYTES__"', literal
    )
    manifest = tmp_path / f"manifest-{literal}.json"
    manifest.write_text(raw, encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "storage_requirement.py"),
            "--manifest",
            str(manifest),
            "--profile",
            "studio",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )

    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    assert "OverflowError" not in done.stderr, done.stderr
    # El marcador de éxito concreto: el requisito se publica y el base ilegible
    # cuenta 0. Sin esta línea, un rc=0 cualquiera —uno que ya no derivara
    # `task_models`— dejaría la prueba pasando en vacío.
    components = json.loads(done.stdout)["components"]
    assert components["task_models"] == 0, literal


def test_a_meta_that_is_not_a_map_does_not_kill_the_storage_script(
    tmp_path: Path,
) -> None:
    """R1: `meta` también lo escribe el operario, y no tiene por qué ser un mapa.

    `manifest.get("meta", {})` devuelve lo que haya, y un `"meta": []` respondía
    `AttributeError` en el `.get("default_bytes")` que seguía. Es la misma
    superficie que el literal no finito, así que degrada igual.
    """
    manifest = tmp_path / "manifest-meta-raro.json"
    manifest.write_text('{"meta": [], "models": []}', encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "storage_requirement.py"),
            "--manifest",
            str(manifest),
            "--profile",
            "studio",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )

    assert done.returncode in (0, 1), done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    assert "AttributeError" not in done.stderr, done.stderr


class _PathLikeStageDir:
    """Un `os.PathLike` que no es un `Path`: lo que `_unsafe_path` acepta."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __fspath__(self) -> str:
        return str(self._path)


@pytest.mark.parametrize("forma", ["str", "pathlike"])
def test_a_stage_dir_that_is_a_string_or_a_path_like_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, forma: str
) -> None:
    """R2: `stage_dir` se coerciona a `Path`, no aborta con `TypeError`.

    `_unsafe_path` acepta `str` y `os.PathLike`, pero `stage` se usa como
    `Path` (`stage / f"job-<pid>-<seq>"`), así que un `str` llegaba al operando
    `/` y respondía `TypeError: unsupported operand type(s) for /: str and str`
    —fuera de todo `except (OSError, ValueError)`, con el plan abortado—. El
    docstring de `acquire()` decía que un kwarg mal tipado era una entrada de
    fallo; para este caso no lo era. Se coerciona una vez, al entrar.
    """
    stage = tmp_path / "stage"
    stage_dir: object = str(stage) if forma == "str" else _PathLikeStageDir(stage)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    local_dirs: list[Path] = []

    def _download(argv: list[str], *, timeout: int, should_cancel: object) -> int:
        local_dirs.append(Path(argv[argv.index("--local-dir") + 1]))
        return _counting_download(FAKE_CONTENT, downloads)(
            argv, timeout=timeout, should_cancel=should_cancel
        )

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download)

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage_dir,  # type: ignore[arg-type]
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads.get("n", 0) == 1
    assert report.stage_dir == str(stage), forma
    assert local_dirs and stage in local_dirs[0].parents, (
        f"el `--local-dir` cayó bajo el directorio pedido, coercido a Path: {local_dirs}"
    )


@pytest.mark.parametrize("progress", [7, "si", [1], object()])
def test_a_progress_that_is_not_callable_becomes_an_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, progress: object
) -> None:
    """R2: `progress` no se validaba, y su `TypeError` salía de `acquire()`.

    El hook se invoca en `_acquire_one` (cuatro veces), así que un
    `progress=7` respondía `TypeError: 'int' object is not callable` **después**
    de haber empezado a trabajar. Misma regla que `should_cancel`, que D5 ya
    validaba: `None` o invocable, y cualquier otra cosa es una entrada de fallo
    por necesidad antes de tocar el disco.
    """
    stage = tmp_path / "stage"
    needs = [
        _acquire_need(
            filename="out.bin",
            dest_path=str(tmp_path / "out.bin"),
            bytes=len(FAKE_CONTENT),
            remote_path="out.bin",
            sha256=_digest(),
        )
    ]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        needs,
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
        progress=progress,  # type: ignore[arg-type]
    )

    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"
    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert "progress" in (entry.error or ""), entry.error
    assert report.failed, "el informe dice qué falló"


@pytest.mark.parametrize("bad_path", [["/tmp", "out.bin"], 7])
@pytest.mark.parametrize("camino", ["kwargs", "sin-cliente"])
def test_a_dest_path_that_is_not_a_path_is_never_published_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_path: object,
    camino: str,
) -> None:
    """R3: el valor rechazado no se publica con el rechazo.

    `AcquiredEntry.dest_path` está tipado `str` y `to_public_dict()` lo publica
    tal cual, pero los caminos que construyen la entrada **fuera** de
    `_acquire_one` pasaban `need.dest_path` sin coercionar: el rechazo de kwargs
    de D5 y el camino sin cliente. `_unsafe_path` rechazaba el valor y el propio
    rechazo lo llevaba en el informe público y en el JSON que lee el agente. Se
    coerciona en `_failure_entry`, por donde pasan todos.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = bad_path  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    if camino == "kwargs":
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0"},
            hf_cli="/fake/hf",
            stage_dir=stage,
            timeout_s="3600",
        )
    else:
        report = asset_acquire.acquire(
            [need],
            env={"HF_DISK_GUARD": "0", "HF_CLI": "", "PATH": ""},
        )

    entry = report.entries[0]
    assert isinstance(entry.dest_path, str), (camino, bad_path, entry.dest_path)
    published = report.to_public_dict()["entries"][0]
    assert isinstance(published["dest_path"], str), published
    json.dumps(published), "el informe público se serializa"
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"


@pytest.mark.parametrize("staged", ["/", "/..", "//"])
def test_remove_staged_of_a_nameless_path_calls_no_rmtree(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, staged: str
) -> None:
    """R4: `rmtree('/')` es un fallo peor que el `ValueError` que tapó D6.

    `_remove_staged` borra con `shutil.rmtree(..., ignore_errors=True)`, así que
    sobre una ruta sin nombre arrasa el árbol entero **sin ruido**: ni excepción
    ni `warning`. Inalcanzable desde `acquire()`, pero la limpieza de la caché
    del cliente y quien prueba los ayudantes la llaman directamente, y un
    borrado best-effort no puede ser destructivo por definición. Se intercepta
    `shutil.rmtree` para comprobarlo sin ejecutarlo.
    """
    rmtree_calls: list[str] = []

    def _recording_rmtree(path, *args, **kwargs):  # noqa: ANN001
        rmtree_calls.append(str(path))

    monkeypatch.setattr(asset_acquire.shutil, "rmtree", _recording_rmtree)

    with caplog.at_level(logging.WARNING, logger="comfy_studio.asset_acquire"):
        asset_acquire._remove_staged(Path(staged))

    assert rmtree_calls == [], staged
    assert any("no nombra un fichero" in r.getMessage() for r in caplog.records), (
        caplog.text
    )


def test_remove_staged_still_removes_a_named_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: la guarda no desactiva la limpieza, que es su razón de ser.

    Un `remote_path` de manifiesto sin extensión se materializa como
    directorio, y borrarlo es lo que hace `_remove_staged`.
    """
    tree = tmp_path / "stage" / "job-1" / "Modelfile"
    tree.mkdir(parents=True)
    (tree / "config.json").write_bytes(b"{}")
    rmtree_calls: list[str] = []

    def _recording_rmtree(path, *args, **kwargs):  # noqa: ANN001
        rmtree_calls.append(str(path))

    monkeypatch.setattr(asset_acquire.shutil, "rmtree", _recording_rmtree)

    asset_acquire._remove_staged(tree)

    assert rmtree_calls == [str(tree)], rmtree_calls


def test_an_install_marker_with_a_boolean_size_never_authorises_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5: `isinstance(True, int)` es verdadero, y un booleano no es un tamaño.

    Una marca editada a mano con `{"bytes": true}` y un manifiesto que declara
    `bytes: 1` autorizaba la reutilización por `True == 1` y publicaba
    `"bytes": true` en el informe: el invariante «ningún tamaño sin verificar se
    hace eco» se saltaba por la puerta de atrás. El destino de esta prueba no se
    deja medir —es el caso para el que la marca existe—, así que la marca es la
    única prueba que había.
    """
    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x")
    (dest.parent / "out.bin.installed").write_text(
        json.dumps({"installed": True, "bytes": True}), encoding="utf-8"
    )
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        # `True == 1`: sin excluir el booleano, la marca autoriza la reutilización.
        bytes=1,
        remote_path="out.bin",
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(b"x", downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert downloads.get("n", 0) == 1, "un booleano no autoriza la reutilización"
    published = report.to_public_dict()["entries"][0]
    assert not isinstance(published["bytes"], bool), published
    # Lo que se publica es el tamaño **medido** del stageado que se acaba de
    # bajar —el `b"x"` del cliente de mentira—, no el `true` de la marca: el 1
    # sale de haber medido lo descargado, y la marca no autorizó nada.
    assert published["bytes"] == len(b"x") == entry.bytes, published


def test_an_unmeasurable_destination_reports_the_staged_size_it_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6/E4: el docstring y el código dicen lo mismo sobre el tamaño informado.

    El módulo afirmaba «se informa del tamaño desconocido» para un destino que
    no se deja medir después de instalar, pero el código publica el tamaño
    **del stageado** —el que sí se midió, antes del `rename`— y la marca de
    `<dest>.installed` lo registra con el mismo valor. Es el comportamiento
    defendible, así que se corrige el texto y esta prueba lo fija por los dos
    lados. E4, la otra candidata, era la afirmación de
    `capabilities._declared_bytes` («misma regla que `model_assets.class_bytes()`
    y que `asset_acquire._declared_bytes`»): D4 la hizo cierta, y aquí queda
    pinchada.
    """
    from comfy_studio import capabilities, model_assets

    doc = asset_acquire.__doc__ or ""
    assert "tamaño desconocido" not in doc, doc
    assert "del stageado" in doc, doc
    infinite = float("inf")
    assert capabilities._declared_bytes(infinite) == 0, "E4: la regla compartida"
    assert asset_acquire._declared_bytes(_acquire_need(bytes=infinite)) == 0
    non_finite = tmp_path / "manifest-infinito.json"
    non_finite.write_text(
        '{"models": [{"filename": "raro.bin", "dest_path": "models/raro.bin",'
        ' "bytes": Infinity, "class": "task"}]}',
        encoding="utf-8",
    )
    assert model_assets.class_bytes({"STUDIO_MANIFEST": str(non_finite)})["task"] == 0, (
        "E4: los tres lectores degradan igual un tamaño no finito"
    )

    stage = tmp_path / "stage"
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )
    _no_sleep(monkeypatch)
    _break_dest_stat_forever(
        monkeypatch, dest, OSError(errno.EIO, "Input/output error")
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "downloaded", entry.error
    assert entry.bytes == len(FAKE_CONTENT), "se informa el tamaño del stageado"
    marker = json.loads((dest.parent / "out.bin.installed").read_text(encoding="utf-8"))
    assert marker["bytes"] == len(FAKE_CONTENT), "la marca registra el mismo tamaño"


# --- Cierre S1-S5 ------------------------------------------------------------


@pytest.mark.parametrize("bad_path", [["/tmp", "out.bin"], 7, {"a": 1}, {"s"}])
@pytest.mark.parametrize("bad_name", [7, ["out.bin"], {"s"}])
def test_a_cancelled_job_never_publishes_a_raw_dest_path_or_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_path: object,
    bad_name: object,
) -> None:
    """R3/S1: la cancelación a mitad de camino tampoco publica valores crudos.

    El camino de cancelación del bucle de `acquire()` construía su
    `AcquiredEntry` a mano con `dest_path=remaining.dest_path` y
    `filename=remaining.filename`, así que se saltaba la coerción de
    `_failure_entry`: un `dest_path` que era una lista, un entero, un diccionario
    o un conjunto salía **tal cual** en `to_public_dict()`, y un conjunto hacía
    fallar `json.dumps` del informe entero. Es la cuarta construcción manual de
    entradas, y pasa por `_public_dest_path`/`_public_filename` como las demás.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = bad_path  # type: ignore[assignment]
    need.filename = bad_name  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        should_cancel=lambda: True,
    )

    assert report.cancelled is True
    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert isinstance(entry.dest_path, str), (bad_path, entry.dest_path)
    assert isinstance(entry.filename, str), (bad_name, entry.filename)
    published = report.to_public_dict()["entries"][0]
    assert isinstance(published["dest_path"], str), published
    assert isinstance(published["filename"], str), published
    json.dumps(report.to_public_dict()), "el informe público se serializa"
    assert downloads.get("n", 0) == 0, "cancelado antes de lanzar el cliente"


def test_a_path_like_hf_cli_is_published_as_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3/S3: `hf_cli` está tipado `str | None` y se publica sin transformar.

    `_unsafe_path` acepta `os.PathLike` además de `str` —es lo que permite que
    `HF_CLI` sea un `Path`—, así que un `PathLike` llegaba tal cual al informe y
    `json.dumps` del dict público respondía `TypeError`. Misma invariante que R3
    sobre `dest_path`: el informe público se serializa.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli=Path("/fake/hf"),
        stage_dir=stage,
        max_attempts=1,
    )

    assert report.entries[0].status == "downloaded", report.entries[0].error
    assert isinstance(report.hf_cli, str), report.hf_cli
    published = report.to_public_dict()
    assert isinstance(published["hf_cli"], str), published
    json.dumps(published), "el informe público se serializa"


def test_a_plain_string_hf_cli_is_published_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3/S3 (regresión): la coerción no cambia el `str` de siempre."""
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, {})
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=stage,
        max_attempts=1,
    )

    assert report.hf_cli == "/fake/hf", report.hf_cli
    assert report.to_public_dict()["hf_cli"] == "/fake/hf"


@pytest.mark.parametrize("bad_path", [["/tmp", "out.bin"], 7, {"a": 1}, {"s"}])
def test_a_non_path_dest_path_inside_acquire_one_is_published_as_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_path: object
) -> None:
    """R3/S5: el rechazo de R97 dentro de `_acquire_one`, pinchado directo.

    La coerción de `_failure_entry` ya estaba verificada por sonda, pero ningún
    test llegaba a `_acquire_one` con un `dest_path` que no es una ruta: solo se
    veía por los caminos de `acquire()` que fallan antes. Aquí se llama a
    `_acquire_one` directamente, que es donde R97 rechaza el campo.
    """
    stage = tmp_path / "stage"
    need = _acquire_need(
        filename="out.bin",
        dest_path=str(tmp_path / "out.bin"),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.dest_path = bad_path  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    entry = asset_acquire._acquire_one(
        need,
        cli="/fake/hf",
        stage=stage,
        timeout=60,
        margin=0,
        max_attempts=1,
        guard_enabled=False,
        progress=None,
        should_cancel=None,
    )

    assert entry.status == "failed", entry.error
    assert "dest_path no es una ruta" in (entry.error or ""), entry.error
    assert isinstance(entry.dest_path, str), (bad_path, entry.dest_path)
    published = entry.to_public_dict()
    assert isinstance(published["dest_path"], str), published
    json.dumps(published), "la entrada pública se serializa"
    assert downloads.get("n", 0) == 0, "se rechaza antes de lanzar el cliente"


@pytest.mark.parametrize("raro", [[], "x", 5, None])
def test_a_models_entry_that_is_not_a_map_does_not_kill_the_storage_script(
    tmp_path: Path, raro: object
) -> None:
    """D4/S2: `models[]` también lo escribe el operario.

    Los cuatro lectores de `scripts/storage_requirement.py` hacían
    `entry.get(...)` fuera de todo `try`, así que una entrada que no es un mapa
    respondía `AttributeError` y el script salía con rc=1 y una traza, sin
    requisito que publicar. Una entrada sin mapa no tiene campos: cuenta 0 y el
    requisito se deriva con el resto.
    """
    manifest = tmp_path / "manifest-models-raro.json"
    manifest.write_text(
        json.dumps({"models": [raro]}),
        encoding="utf-8",
    )
    # El core se aísla a propósito: sin `--core-manifest`, el script resuelve el
    # `config/models-core.json` del repo y `staging_peak` recoge su pico a
    # demanda real, que es correcto pero esconde lo que esta prueba mira.
    empty_core = tmp_path / "core-vacio.json"
    empty_core.write_text(json.dumps({"models": []}), encoding="utf-8")

    done = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "storage_requirement.py"),
            "--manifest",
            str(manifest),
            "--core-manifest",
            str(empty_core),
            "--profile",
            "studio",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )

    assert "Traceback" not in done.stderr, done.stderr
    assert "AttributeError" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    components = json.loads(done.stdout)["components"]
    assert components["task_models"] == 0, raro
    assert components["staging_peak"] == 0, raro


# --- Cierre T1: `filename` no textual en las salidas de `_acquire_one` --------


@pytest.mark.parametrize("raro", [{"s"}, 7, ["a"], None])
def test_a_non_text_filename_is_published_as_a_string_when_the_destination_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raro: object
) -> None:
    """T1: el `skipped` de destino reutilizable publicaba el `filename` crudo.

    `_failure_entry` y la cancelación del bucle de `acquire()` coercenan `filename`
    con `_public_filename`, pero las **diez** construcciones a mano de
    `AcquiredEntry` dentro de `_acquire_one` —el destino reusable (`skipped`), la
    cancelación del rc 130, la cancelación y el fallo del veredicto del stageado,
    la cancelación y el fallo antes del `rename`, la cancelación y el fallo del
    `rename`, el `downloaded` que no pudo medir el destino y el `downloaded`
    bueno— pasaban `filename=need.filename` tal cual. Un nombre que no es texto
    acababa en `to_public_dict()["entries"]` y el `json.dumps` del informe entero
    respondía `TypeError: Object of type set is not JSON serializable`.

    La sonda es el camino más barato de los diez: con un destino reusable no hay
    descarga, así que el fallo solo puede venir de la construcción de la entrada.
    """
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.filename = raro  # type: ignore[assignment]
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, {})
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=tmp_path / "stage",
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "skipped", entry.error
    assert isinstance(entry.filename, str), (raro, entry.filename)
    published = report.to_public_dict()
    assert isinstance(published["entries"][0]["filename"], str), published
    # El invariante que importa: el informe entero se serializa.
    json.dumps(published), f"el informe público se serializa con filename={raro!r}"


@pytest.mark.parametrize("raro", [{"s"}, 7])
def test_a_non_text_filename_in_a_failed_entry_is_published_as_a_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raro: object
) -> None:
    """T1: el `failed` del veredicto del stageado publicaba el nombre crudo.

    Es la otra mitad del agujero, y la que ve el agente: con un `sha256` que no
    cuadra, `_check_staged` devuelve el veredicto de fallo y `_acquire_one`
    construye la entrada a mano. `to_public_dict()["failed"]` es la lista de
    **nombres**, así que un nombre no textual iba directo a la lista que el
    agente lee y al `json.dumps` que la empaqueta.
    """
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256="0" * 64,
    )
    need.filename = raro  # type: ignore[assignment]
    downloads: dict[str, int] = {}
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, downloads)
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=tmp_path / "stage",
        max_attempts=1,
    )

    entry = report.entries[0]
    assert entry.status == "failed", entry.error
    assert downloads.get("n", 0) == 1, "el hash no cuadra: se baja y se verifica"
    assert isinstance(entry.filename, str), (raro, entry.filename)
    published = report.to_public_dict()
    assert published["failed"] == [entry.filename], published["failed"]
    assert all(isinstance(name, str) for name in published["failed"]), published
    json.dumps(published), f"el informe público se serializa con filename={raro!r}"


def test_progress_receives_the_published_filename_not_the_manifest_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T1: `progress` está tipado `Callable[[str, str], None]`, no `object`.

    Las mismas diez construcciones venían con un `progress(need.filename, ...)`
    al lado. Coercer solo la entrada dejaba el nombre crudo en el callback, que
    es dato de manifiesto igual que el resto: un llamador que lo registre o lo
    serialize se encuentra con el mismo valor no textual.
    """
    dest = tmp_path / "models" / "out.bin"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(FAKE_CONTENT)
    need = _acquire_need(
        dest_path=str(dest),
        bytes=len(FAKE_CONTENT),
        remote_path="out.bin",
        sha256=_digest(),
    )
    need.filename = {"s"}  # type: ignore[assignment]
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        asset_acquire, "_run_hf_download", _counting_download(FAKE_CONTENT, {})
    )

    report = asset_acquire.acquire(
        [need],
        env={"HF_DISK_GUARD": "0"},
        hf_cli="/fake/hf",
        stage_dir=tmp_path / "stage",
        max_attempts=1,
        progress=lambda filename, state: events.append((filename, state)),
    )

    assert report.entries[0].status == "skipped", report.entries[0].error
    assert events, "el destino reusable también avisa del salto"
    assert all(isinstance(name, str) for name, _ in events), events


# --- Cierre T2: las cinco formas de dato de operario que quedaban sin guarda --


def _run_storage_script(
    manifest: Path, core_manifest: Path, *, profile: str = "studio"
) -> subprocess.CompletedProcess[str]:
    """El script de requisito, tal y como lo lanza CI o un operario."""
    return subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "storage_requirement.py"),
            "--manifest",
            str(manifest),
            "--core-manifest",
            str(core_manifest),
            "--profile",
            profile,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )


@pytest.mark.parametrize("models", [5, None, {"a": 1}, "x"])
def test_a_models_key_that_is_not_a_list_does_not_kill_the_storage_script(
    tmp_path: Path, models: object
) -> None:
    """T2: `models` no tiene por qué ser una lista.

    `manifest.get("models", [])` devuelve lo que haya, y el `for entry in ...`
    de los cuatro lectores estaba fuera de todo `try`: un `"models": 5` salía por
    `TypeError: 'int' object is not iterable` y un `"models": null` por el mismo
    sitio —la clave existe, así que ni siquiera pasaba por el default—. Un
    `models` que no es una lista no tiene entradas: cuenta vacío, avisa, y el
    requisito se publica con el resto de componentes.
    """
    manifest = tmp_path / "manifest-models-no-lista.json"
    manifest.write_text(json.dumps({"models": models}), encoding="utf-8")
    core = tmp_path / "core-vacio.json"
    core.write_text('{"models": []}', encoding="utf-8")

    done = _run_storage_script(manifest, core)

    assert "Traceback" not in done.stderr, done.stderr
    assert "TypeError" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    assert "AVISO" in done.stderr, done.stderr
    components = json.loads(done.stdout)["components"]
    assert components["task_models"] == 0, models
    assert components["staging_peak"] == 0, models


@pytest.mark.parametrize("raiz", [[], 5, "x", None])
def test_a_manifest_root_that_is_not_an_object_does_not_kill_the_storage_script(
    tmp_path: Path, raiz: object
) -> None:
    """T2: la raíz del manifiesto también la escribe el operario.

    `json.load` devuelve lo que haya en el fichero, y una raíz que no es un
    objeto llega a `manifest.get("meta", {})` y al `for` de `models`:
    `AttributeError: 'list' object has no attribute 'get'`, que no es ni
    `TypeError` ni `ValueError`, con el script saliendo por traza y sin requisito
    que publicar. Una raíz sin objeto no tiene nada que derivar: se degrada a
    manifiesto vacío y se avisa.
    """
    manifest = tmp_path / "manifest-raiz-rara.json"
    manifest.write_text(json.dumps(raiz), encoding="utf-8")
    core = tmp_path / "core-vacio.json"
    core.write_text('{"models": []}', encoding="utf-8")

    done = _run_storage_script(manifest, core)

    assert "Traceback" not in done.stderr, done.stderr
    assert "AttributeError" not in done.stderr, done.stderr
    assert "TypeError" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    assert "AVISO" in done.stderr, done.stderr
    # El requisito se publica: no es un rc=0 vacío.
    components = json.loads(done.stdout)["components"]
    assert components["task_models"] == 0, raiz


@pytest.mark.parametrize("raiz", [[], None])
def test_a_falsy_core_root_that_is_not_an_object_is_reported(
    tmp_path: Path, raiz: object
) -> None:
    """T2 (residual): la raíz falsy del catálogo core también avisa.

    `requirement_bytes` leía el catálogo core con `core_manifest or {}`, así que
    una raíz `[]` o `null` —las mismas formas no-objeto que un `5` o un `"x"`—
    se degradaba a vacío **en silencio**: el operario no se enteraba de que su
    catálogo core no se estaba leyendo, mientras que las raíces verdaderas sí
    avisaban por `_manifest_root`. Se lee la raíz por el mismo helper que el
    resto, y el aviso sale para todas las formas.
    """
    manifest = tmp_path / "manifest-vacio.json"
    manifest.write_text('{"models": []}', encoding="utf-8")
    core = tmp_path / "core-raiz-falsa.json"
    core.write_text(json.dumps(raiz), encoding="utf-8")

    done = _run_storage_script(manifest, core)

    assert "Traceback" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    assert "AVISO" in done.stderr, done.stderr
    assert "manifiesto sin objeto en la raíz" in done.stderr, done.stderr
    components = json.loads(done.stdout)["components"]
    assert components["startup_download"] == 0, raiz
    assert components["staging_peak"] == 0, raiz


def test_a_falsy_core_manifest_root_is_reported_to_a_direct_caller(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2 (residual): `requirement_bytes` lee la raíz core por `_manifest_root`.

    Es la otra mitad del agujero. El script normaliza la raíz al cargarla
    (`_load_core_manifest`), pero `requirement_bytes` es público y un llamador
    directo puede pasar el catálogo ya leído: con `core_manifest or {}` una raíz
    `[]` se degradaba en silencio mientras un `5` —la misma forma no-objeto, solo
    que verdadera— avisaba. Ahora las dos avisan, y los números son los de no
    tener catálogo, que es lo que una raíz ilegible significa.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import storage_requirement

    # El aviso es una sola vez por mensaje: se limpia para que la prueba no
    # dependa de quién avisó antes en el mismo proceso.
    storage_requirement._WARNED.clear()
    falsy = storage_requirement.requirement_bytes(
        {"models": []}, profile="studio", core_manifest=[]
    )
    assert "manifiesto sin objeto en la raíz" in capsys.readouterr().err

    storage_requirement._WARNED.clear()
    absent = storage_requirement.requirement_bytes(
        {"models": []}, profile="studio", core_manifest=None
    )
    assert capsys.readouterr().err == "", "sin catálogo core no hay raíz que avisar"

    assert falsy["components"] == absent["components"]
    assert falsy["total_bytes"] == absent["total_bytes"]


def test_an_unhashable_opt_in_does_not_kill_the_storage_script(tmp_path: Path) -> None:
    """T2: `opt_in` acaba siendo clave de un diccionario.

    `out[flag] = ...` sobre un `"opt_in": ["a"]` responde
    `TypeError: unhashable type: 'list'`. El grupo no se puede indexar, así que
    se ignora ese grupo —no el manifiesto— y el resto se deriva igual.
    """
    manifest = tmp_path / "manifest-opt-in-raro.json"
    manifest.write_text(
        json.dumps({"models": [{"opt_in": ["a"], "bytes": 10}, {"bytes": 20}]}),
        encoding="utf-8",
    )
    core = tmp_path / "core-vacio.json"
    core.write_text('{"models": []}', encoding="utf-8")

    done = _run_storage_script(manifest, core)

    assert "Traceback" not in done.stderr, done.stderr
    assert "TypeError" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    assert "AVISO" in done.stderr, done.stderr
    components = json.loads(done.stdout)["components"]
    # La entrada sin `opt_in` sí se lee: el grupo ilegible se salta, no el catálogo.
    # (`task_models` es `meta.default_bytes` + los opt-ins **pedidos**, y aquí no se
    # pide ninguno, así que la prueba de que el resto se derivó es el staging.)
    assert components["task_models"] == 0, components["task_models"]
    assert components["staging_peak"] == 40, components["staging_peak"]


@pytest.mark.parametrize(
    "raro",
    [{"class": [], "bytes": 10}, {"class": "task", "profiles": 5, "bytes": 10}],
)
def test_an_unreadable_core_entry_does_not_kill_the_storage_script(
    tmp_path: Path, raro: dict[str, object]
) -> None:
    """T2: `class` y `profiles` del catálogo core también son de operario.

    El pico de staging a demanda hacía `_entry_field(entry, "class") in
    ON_DEMAND_CLASSES` y `{str(p) for p in entry["profiles"]}` fuera de todo
    `try`: una `class` que es una lista responde
    `TypeError: unhashable type: 'list'` y un `profiles` que no es iterable
    `TypeError: 'int' object is not iterable`. Los dos se leen solo para perfiles
    que no son de Fase 3, que es por donde se llega aquí (`general`), y el
    segundo solo si la entrada además es de clase a demanda.

    Una `class` ilegible no está en el conjunto de clases a demanda, y un
    `profiles` ilegible es «sin perfiles declarados»: la entrada cuenta para
    todos, que es lo que significa el `not _entry_field(...)` de al lado.
    """
    core = tmp_path / "core-raro.json"
    core.write_text(json.dumps({"models": [raro]}), encoding="utf-8")
    manifest = tmp_path / "manifest-vacio.json"
    manifest.write_text('{"models": []}', encoding="utf-8")

    done = _run_storage_script(manifest, core, profile="general")

    assert "Traceback" not in done.stderr, done.stderr
    assert "TypeError" not in done.stderr, done.stderr
    assert done.returncode == 0, done.stderr
    assert "AVISO" in done.stderr, done.stderr
    components = json.loads(done.stdout)["components"]
    if "profiles" in raro:
        # Sin perfiles declarados la entrada cuenta: 10 bytes × factor 2.
        assert components["staging_peak"] == 20, components["staging_peak"]
    else:
        # Una clase que no se puede comparar no es a demanda: no cuenta.
        assert components["staging_peak"] == 0, components["staging_peak"]
