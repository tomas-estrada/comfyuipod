"""Tests del resolvedor de capacidades y de la adquisición de activos.

Verifican el flujo que define la política de arquitectura:

  «necesito esta capacidad»
  → el motor ya está preparado;
  → el resolvedor identifica sus activos soportados;
  → los que faltan se descargan con la herramienta existente;
  → se selecciona una receta;
  → los activos reutilizables se conservan (segunda pasada = skip).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from comfy_studio import asset_acquire
from comfy_studio.capabilities import (
    CAPABILITIES,
    CAPABILITY_REGISTRY_VERSION,
    AssetNeed,
    list_capabilities,
    needs_for_group,
    resolve,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("COMFY_DATA_DIR", str(root))
    return root


#: Contenido que escribe el `hf` de mentira. Su hash se fija en los activos
#: para que la verificación de integridad del manifiesto pase.
FAKE_CONTENT = b"pesos-de-mentira"


def _write_fake_hf(tmp_path: Path, payload: bytes = FAKE_CONTENT) -> Path:
    """Cliente `hf` de mentira, portable (Python, no bash).

    Replica `hf download <repo> <remote_path> ... --local-dir <dir>`: escribe
    `payload` en `<dir>/<remote_path>`. Se usa un launcher Python en vez de un
    script bash para que la suite corra igual en Linux (CI/pod) y en
    Windows/macOS (desarrollo). El pod de producción es Linux; el cliente real
    es el `hf` de verdad, este fake solo ejercita el cableado de adquisición.
    """
    helper = tmp_path / "fake_hf_impl.py"
    helper.write_text(
        "import sys, pathlib\n"
        "args = sys.argv[1:]\n"
        "remote = args[2]\n"
        "local_dir = args[args.index('--local-dir') + 1]\n"
        "payload = bytes.fromhex(args[args.index('--__payload_hex') + 1])"
        " if '--__payload_hex' in args else b'" + payload.decode() + "'\n"
        "target = pathlib.Path(local_dir) / remote\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "target.write_bytes(payload)\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = tmp_path / "fake-hf.cmd"
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{helper}" %*\r\n', encoding="utf-8"
        )
    else:
        launcher = tmp_path / "fake-hf"
        launcher.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{helper}" "$@"\n', encoding="utf-8"
        )
        launcher.chmod(launcher.stat().st_mode | stat.S_IEXEC)
    return launcher


@pytest.fixture()
def fake_hf(tmp_path: Path) -> Path:
    """Cliente `hf` de mentira: crea el archivo remoto bajo --local-dir."""
    return _write_fake_hf(tmp_path)


def _env(fake_hf: Path, data_dir: Path) -> dict[str, str]:
    return {"COMFY_DATA_DIR": str(data_dir), "HF_CLI": str(fake_hf)}


def _pin_fake_hashes(resolution) -> None:
    """Alinea el hash declarado con lo que escribe el `hf` de mentira."""
    import hashlib

    digest = hashlib.sha256(FAKE_CONTENT).hexdigest()
    for need in list(resolution.assets) + list(resolution.missing):
        need.sha256 = digest
        need.sha1 = None


# --------------------------------------------------------------------------
# Registro de capacidades
# --------------------------------------------------------------------------


def test_registry_is_versioned_and_non_empty() -> None:
    assert CAPABILITY_REGISTRY_VERSION >= 1
    assert len(CAPABILITIES) >= 15
    for capability_id, spec in CAPABILITIES.items():
        assert spec["engine"], f"{capability_id} sin motor"
        assert spec["summary"], f"{capability_id} sin resumen"
        assert isinstance(spec["templates"], list)


def test_every_registered_template_exists() -> None:
    """Una capacidad que apunta a una plantilla inexistente es un error de datos."""
    from comfy_studio.templates import get_default_registry

    registry = get_default_registry()
    for capability_id, spec in CAPABILITIES.items():
        for template_id in spec["templates"]:
            assert registry.get_raw_definition(template_id) is not None, (
                f"{capability_id} apunta a la plantilla inexistente {template_id!r}"
            )


def test_list_capabilities_is_serializable() -> None:
    blob = json.dumps(list_capabilities())
    assert '"video.text_to_video"' in blob


# --------------------------------------------------------------------------
# Resolución
# --------------------------------------------------------------------------


def test_unknown_capability_is_reported_not_guessed() -> None:
    resolution = resolve("video.does_not_exist")
    assert resolution.state == "unknown_capability"
    assert resolution.templates == []


def test_resolution_reports_engine_prep_and_assets(data_dir: Path) -> None:
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    # El motor no está instalado en el host de tests.
    assert resolution.engine == "comfyui"
    assert resolution.engine_prep["state"] == "not_installed"
    assert resolution.state == "engine_unprepared"
    # Los activos se resuelven igual: es información independiente del motor.
    assert len(resolution.assets) == 4
    assert all(not a.present for a in resolution.assets)
    assert resolution.missing_bytes > 0


def test_assets_carry_absolute_paths_and_manifest_origin(data_dir: Path) -> None:
    resolution = resolve("image.generate", env={"COMFY_DATA_DIR": str(data_dir)})
    need = resolution.assets[0]
    assert need.filename == "Juggernaut-XI-byRunDiffusion.safetensors"
    assert need.source == "core"
    assert need.asset_class == "task"
    assert Path(need.dest_path).is_absolute()
    assert str(data_dir) in need.dest_path
    assert need.manifest_dest.startswith("models/")


def test_optional_group_is_classified_optional(data_dir: Path) -> None:
    resolution = resolve("video.animate", env={"COMFY_DATA_DIR": str(data_dir)})
    classes = {a.filename: a.asset_class for a in resolution.assets}
    assert classes["wan2.2_animate_14B_int8_convrot.safetensors"] == "optional"
    assert classes["umt5_xxl_fp8_e4m3fn_scaled.safetensors"] == "task"


def test_bootstrap_is_empty_by_design(data_dir: Path) -> None:
    """Nada es bootstrap: ni un VAE ni un restaurador facial lo son.

    «Pertenecer a la Fase 1 no convierte un VAE o restaurador facial en requisito
    para abrir la API y descubrir capacidades.» El arranque no descarga nada;
    todo se pide por capacidad. La política prohíbe llamar bootstrap a una
    colección grande, y aquí directamente no hay nada que lo sea.
    """
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    bootstrap = [a for a in resolution.assets if a.asset_class == "bootstrap"]
    assert bootstrap == [], "el arranque no debe arrastrar ningún activo"
    # Lo que antes era «bootstrap» ahora es task: se descarga si se pide.
    task = [a for a in resolution.assets if a.asset_class == "task"]
    assert task, "la capacidad debe seguir declarando sus dependencias"


def test_engine_without_recipe_reports_no_assets(data_dir: Path) -> None:
    resolution = resolve("video.upscale", env={"COMFY_DATA_DIR": str(data_dir)})
    assert resolution.selected_template is None
    assert resolution.assets == []
    assert resolution.engine == "upscaler"


# --------------------------------------------------------------------------
# Adquisición
# --------------------------------------------------------------------------


def test_acquire_downloads_then_reuses(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)

    _pin_fake_hashes(resolution)
    report = asset_acquire.acquire(resolution.missing, env=env)
    assert not report.failed
    assert all(e.status == "downloaded" for e in report.entries)
    assert report.downloaded_bytes > 0
    # Los archivos existen donde el manifiesto dice.
    for need in resolution.missing:
        assert Path(need.dest_path).is_file()

    # Segunda pasada: todo se reutiliza, cero descargas.
    second = asset_acquire.acquire(resolution.missing, env=env)
    assert all(e.status == "skipped" for e in second.entries)
    assert second.downloaded_bytes == 0

    # Y el resolvedor ya no ve activos faltantes.
    after = resolve("video.face_restore", env=env)
    assert after.missing == []


def test_acquire_never_writes_outside_the_data_dir(data_dir: Path, fake_hf: Path) -> None:
    """Un destino no absoluto se rechaza: nada de escrituras en el repo."""
    env = _env(fake_hf, data_dir)
    bogus = AssetNeed(
        filename="x.bin",
        dest_path="models/checkpoints/x.bin",
        bytes=10,
        asset_class="task",
        present=False,
        source="core",
        repo_id="repo/x",
        remote_path="x.bin",
    )
    report = asset_acquire.acquire([bogus], env=env)
    assert report.entries[0].status == "failed"
    assert "no absoluto" in (report.entries[0].error or "")
    assert not (REPO_ROOT / "models").exists()


def test_acquire_without_hf_client_fails_closed(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sin cliente de HuggingFace resoluble (portable: no depende de que el PATH
    # del OS de desarrollo carezca de un `hf`), la adquisición falla cerrada.
    monkeypatch.setattr(asset_acquire, "resolve_hf_cli", lambda env=None: None)
    env = {"COMFY_DATA_DIR": str(data_dir)}
    report = asset_acquire.acquire(
        [
            AssetNeed(
                filename="x.bin",
                dest_path=str(data_dir / "x.bin"),
                bytes=10,
                asset_class="task",
                present=False,
                source="core",
                repo_id="repo/x",
                remote_path="x.bin",
            )
        ],
        env=env,
    )
    assert report.entries[0].status == "failed"
    assert "HuggingFace" in (report.entries[0].error or "")


def test_disk_guard_fails_closed_when_space_is_short(data_dir: Path, fake_hf: Path) -> None:
    """Con margen enorme y disco justo, la descarga se rechaza antes de empezar."""
    env = _env(fake_hf, data_dir)
    need = AssetNeed(
        filename="grande.bin",
        dest_path=str(data_dir / "grande.bin"),
        bytes=1024**3,
        asset_class="task",
        present=False,
        source="core",
        repo_id="repo/x",
        remote_path="grande.bin",
    )
    report = asset_acquire.acquire([need], env=env, margin_bytes=10**15)
    assert report.entries[0].status == "failed"
    assert "espacio insuficiente" in (report.entries[0].error or "")


def test_cancellation_stops_between_entries(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    needs = needs_for_group("STUDIO_INCLUDE_ANIMATE", env=env)
    assert needs, "el grupo animate debe declarar activos"
    report = asset_acquire.acquire(
        needs, env=env, should_cancel=lambda: True
    )
    assert report.cancelled is True
    assert report.entries == []


def test_progress_callback_reports_each_phase(data_dir: Path, fake_hf: Path) -> None:
    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)
    events: list[tuple[str, str]] = []
    asset_acquire.acquire(
        resolution.missing, env=env, progress=lambda f, s: events.append((f, s))
    )
    assert events, "debe emitir progreso"
    assert any(state == "downloaded" for _, state in events)


def test_group_needs_follow_the_manifest(data_dir: Path) -> None:
    env = {"COMFY_DATA_DIR": str(data_dir)}
    for flag, expected_count in (
        ("STUDIO_INCLUDE_ANIMATE", 1),
        ("STUDIO_INCLUDE_3D", 1),
        ("STUDIO_INCLUDE_MINIMAX", 6),
    ):
        needs = needs_for_group(flag, env=env)
        assert len(needs) == expected_count, flag
        assert all(n.source == "studio" for n in needs)


# --------------------------------------------------------------------------
# Integración con el tool-belt del agente
# --------------------------------------------------------------------------


def test_belt_exposes_the_new_tools() -> None:
    from comfy_studio.agent.tools import create_default_tool_belt

    names = set(create_default_tool_belt().list_names())
    assert {"list_capabilities", "request_capability", "download_model"}.issubset(names)


def test_request_capability_tool_acquires_and_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El flujo completo de la política, por la herramienta del agente."""
    import asyncio

    from comfy_studio.agent.tools import (
        RequestCapabilityArgs,
        _handle_request_capability,
        create_default_tool_belt,
    )
    from comfy_studio.config import StudioConfig
    from comfy_studio.progress import ProgressBroker
    from comfy_studio.projects import ProjectStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    script = _write_fake_hf(tmp_path, payload=b"pesos")
    os.environ["COMFY_DATA_DIR"] = str(data_dir)
    os.environ["HF_CLI"] = str(script)
    # El motor «preparado» exige un entrypoint real: se simula /opt/ComfyUI.
    app_dir = tmp_path / "ComfyUI"
    app_dir.mkdir()
    (app_dir / "main.py").write_text("# stub\n", encoding="utf-8")
    os.environ["COMFY_APP_DIR"] = str(app_dir)
    store = ProjectStore(str(tmp_path / "studio.db"))
    project = store.create_project("test-capability")
    cfg = StudioConfig.from_env({"STUDIO_DATA_DIR": str(data_dir)})
    context = type(
        "Ctx",
        (),
        {
            "project_id": project["id"],
            "store": store,
            "comfyui_client": None,
            "broker": ProgressBroker(),
            "config": cfg,
        },
    )()

    # Este test cubre el cableado de la herramienta, no la integridad (que tiene
    # tests propios más abajo): se alinea el hash con lo que escribe el fake.
    import comfy_studio.asset_acquire as acquire_mod

    monkeypatch.setattr(acquire_mod, "_verify_integrity", lambda path, need: True)

    result = asyncio.run(
        _handle_request_capability(
            RequestCapabilityArgs(capability="video.face_restore"), context
        )
    )
    assert result["capability"] == "video.face_restore"
    assert result["downloaded"], "debe adquirir los activos que faltaban"
    # Motor preparado + activos en disco = listo para encolar.
    assert result["state"] == "ready"
    assert result["state"] == "ready"
    assert "enqueue_render" in result["next_step"]
    assert result["selected_template"] == "face_fix"

    # Sin acquire: no descarga nada, solo informa.
    again = asyncio.run(
        _handle_request_capability(
            RequestCapabilityArgs(capability="video.face_restore", acquire=False), context
        )
    )
    assert "acquisition" not in again

    # Capacidad desconocida: error tipado, no silencio.
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError, match="no registrada"):
        asyncio.run(
            _handle_request_capability(
                RequestCapabilityArgs(capability="video.nope"), context
            )
        )

    belt = create_default_tool_belt()
    assert "request_capability" in belt.list_names()


# --------------------------------------------------------------------------
# Integridad: revisión fijada y verificación de hash
# --------------------------------------------------------------------------


def test_manifest_assets_carry_revision_and_hash(data_dir: Path) -> None:
    """Cada activo declara la revisión (commit) y el hash que publica HF."""
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    for need in resolution.assets:
        assert need.revision and len(need.revision) == 40, need.filename
        assert need.sha256 or need.sha1, need.filename
        if need.sha256:
            assert len(need.sha256) == 64
        if need.sha1:
            assert len(need.sha1) == 40


def test_acquire_rejects_a_file_whose_hash_does_not_match(
    data_dir: Path, fake_hf: Path
) -> None:
    """Un activo corrupto o sustituido se rechaza, no se instala."""
    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    # El hash del manifiesto es el del fichero real de HuggingFace; el fake
    # escribe otro contenido, así que la verificación debe fallar.
    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    entry = report.entries[0]
    assert entry.status == "failed"
    assert "integridad" in (entry.error or "")
    assert not Path(need.dest_path).exists(), "no debe dejar el fichero en disco"


def test_acquire_accepts_a_file_whose_hash_matches(data_dir: Path, fake_hf: Path) -> None:
    """Con el hash correcto declarado, la descarga se acepta y se reutiliza."""
    import hashlib

    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    need.sha256 = hashlib.sha256(b"pesos-de-mentira").hexdigest()
    need.sha1 = None

    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert report.entries[0].status == "downloaded"
    assert Path(need.dest_path).is_file()

    # Y la segunda pasada lo reutiliza sin volver a descargar.
    second = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert second.entries[0].status == "skipped"


def test_acquire_without_declared_hash_still_works(data_dir: Path, fake_hf: Path) -> None:
    """«Where available»: sin hash se acepta el activo (tamaño ya verificado)."""
    resolution = resolve("video.face_restore", env=_env(fake_hf, data_dir))
    need = resolution.missing[0]
    need.sha256 = None
    need.sha1 = None
    report = asset_acquire.acquire([need], env=_env(fake_hf, data_dir))
    assert report.entries[0].status == "downloaded"


# --------------------------------------------------------------------------
# Cancelación durante la descarga (política, punto 4: «cancellation»)
# --------------------------------------------------------------------------


def test_cancel_midway_reports_cancelled_without_retrying(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """Cancelar durante la descarga no es un fallo: no se reintenta.

    El cliente real se cancela en `_run_hf_download`; aquí se comprueba el
    cableado de `acquire`/`_acquire_one`: estado `cancelled`, staging limpio y
    sin backoff (reintentar un cancelado gastaría ancho de banda inútilmente).
    """
    from comfy_studio import asset_acquire

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)

    # El usuario cancela *durante* la primera descarga, no antes de empezar.
    state = {"cancel": False}

    def _user_cancelled() -> bool:
        return state["cancel"]

    def _download_then_cancel(argv: list[str], **_: object) -> int:
        stage = Path(argv[argv.index("--local-dir") + 1])
        target = stage / argv[3]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"parcial")
        state["cancel"] = True
        return asset_acquire.CANCELLED_RETURNCODE

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _download_then_cancel)

    report = asset_acquire.acquire(
        resolution.missing, env=env, should_cancel=_user_cancelled
    )
    assert report.cancelled is True
    cancelled = [e for e in report.entries if e.status == "cancelled"]
    assert cancelled, [e.status for e in report.entries]
    for entry in cancelled:
        assert entry.attempts == 1, "un cancelado no debe reintentarse"
    # El staging queda limpio para que la recuperación no vea un fichero parcial.
    stage = Path(report.stage_dir)
    assert not any(stage.rglob("*.safetensors")), "quedó un parcial en staging"


@pytest.mark.skipif(
    os.name == "nt",
    reason="usa grupos de proceso POSIX, kill(1) y sleep(1); el pod es Linux",
)
def test_real_download_process_is_killed_on_cancel() -> None:
    """El proceso real se mata en ~1 s, con su árbol y sin tocar grupos ajenos."""
    import os
    import subprocess
    import time

    from comfy_studio.asset_acquire import (
        CANCELLED_RETURNCODE,
        TIMEOUT_RETURNCODE,
        _run_hf_download,
        _terminate_tree,
    )

    me = os.getpid()
    slow = [sys.executable, "-c", "import time; time.sleep(60)"]
    t0 = time.monotonic()
    rc = _run_hf_download(slow, timeout=60, should_cancel=lambda: time.monotonic() - t0 > 1.5)
    assert rc == CANCELLED_RETURNCODE
    assert time.monotonic() - t0 < 10, "la cancelación debe surtir efecto de inmediato"

    t0 = time.monotonic()
    assert _run_hf_download(slow, timeout=2, should_cancel=None) == TIMEOUT_RETURNCODE
    assert time.monotonic() - t0 < 10
    assert (
        _run_hf_download(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            timeout=10,
            should_cancel=None,
        )
        == 3
    )

    # Hijo sin sesión propia: muere con el grupo del cliente.
    script = (
        "import subprocess,sys,time\n"
        "p=subprocess.Popen(['sleep','300'])\n"
        "sys.stdout.write(str(p.pid)+chr(10)); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    child = int(proc.stdout.readline())
    _terminate_tree(proc)
    time.sleep(0.4)
    assert (
        subprocess.run(["kill", "-0", str(child)], capture_output=True).returncode != 0
    ), "quedó un descargador huérfano escribiendo en staging"
    subprocess.run(["kill", "-9", str(child)], capture_output=True)

    # Un proceso del grupo del test NO debe matar al test: `killpg` solo se
    # aplica cuando el cliente liderea su grupo.
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    _terminate_tree(sibling)
    time.sleep(0.3)
    assert subprocess.run(["kill", "-0", str(me)], capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# Filesystems distintos (política, punto 1: «the actual filesystems involved»)
# --------------------------------------------------------------------------


def test_disk_guard_checks_the_destination_filesystem_too(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """Staging y destino pueden estar en volúmenes distintos.

    La guarda miraba solo staging: un destino en otro volumen podía quedarse sin
    espacio cuando el activo ya estaba descargado.
    """
    import shutil

    from comfy_studio import asset_acquire

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    _pin_fake_hashes(resolution)

    real = shutil.disk_usage

    def _fake_disk_usage(path):
        usage = real(path)
        # El staging tiene sitio de sobra; el destino está casi lleno.
        if str(path).endswith(asset_acquire.STAGE_DIRNAME):
            return type(usage)(10**12, 10**12, 0)
        return type(usage)(10, 10, 0)

    monkeypatch.setattr(asset_acquire.shutil, "disk_usage", _fake_disk_usage)
    report = asset_acquire.acquire(resolution.missing, env=env)
    assert report.entries
    assert all(e.status == "failed" for e in report.entries)
    assert any("espacio insuficiente" in (e.error or "") for e in report.entries)


def test_free_bytes_for_takes_the_most_restrictive_filesystem() -> None:
    """Con varios filesystems, manda el que menos sitio tenga."""
    from comfy_studio.asset_acquire import _free_bytes_for

    assert _free_bytes_for([Path("/tmp")]) is not None
    assert _free_bytes_for([]) is None


def test_move_survives_a_cross_device_destination(tmp_path: Path) -> None:
    """EXDEV entre volúmenes: se copia a un temporal same-fs y se publica atómico.

    Solo el `os.replace(src, dest)` del happy path finge EXDEV; el
    `os.replace(tmp, dest)` del fallback (mismo filesystem) es real.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"contenido" * 1000)

    real_replace = real_os.replace

    def _exdev_only_src(a, b):  # noqa: ANN001
        if Path(a) == src:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(a, b)

    real_os.replace = _exdev_only_src
    try:
        _move(src, dest)
    finally:
        real_os.replace = real_replace

    assert dest.read_bytes() == b"contenido" * 1000
    assert not src.exists(), "el original debe desaparecer tras copiarlo"


def test_move_still_propagates_real_errors(tmp_path: Path) -> None:
    """EXDEV se tolera; cualquier otro error de `os.replace` debe salir."""
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"x")

    real_replace = real_os.replace

    def _boom(a, b):  # noqa: ANN001
        raise OSError(13, "Permission denied")

    real_os.replace = _boom
    try:
        raised = False
        try:
            _move(src, dest)
        except OSError:
            raised = True
    finally:
        real_os.replace = real_replace
    assert raised, "un error que no es EXDEV no debe degradarse a copia"


# --------------------------------------------------------------------------
# Semántica del hash: el SHA-1 de un blob de Git NO es el SHA-1 crudo
# --------------------------------------------------------------------------


class _Need:
    """Mínimo que consume `_verify_integrity`."""

    def __init__(self, *, sha256: str | None = None, sha1: str | None = None) -> None:
        self.sha256 = sha256
        self.sha1 = sha1


def test_git_blob_sha1_is_not_the_raw_sha1(tmp_path: Path) -> None:
    """Git antepone `blob <tamaño>\\0` antes de hashear.

    Es la trampa clásica: el SHA-1 crudo de los bytes y el OID del blob de git
    son distintos para el mismo contenido.
    """
    import hashlib

    from comfy_studio.asset_acquire import git_blob_sha1

    target = tmp_path / "fichero.bin"
    content = b"hola mundo\n" * 7
    target.write_bytes(content)

    raw = hashlib.sha1(content).hexdigest()
    blob = git_blob_sha1(target)
    assert blob != raw, "si coinciden, la implementación está hasheando en crudo"
    # Referencia independiente: `git hash-object`.
    assert blob == subprocess.run(
        ["git", "hash-object", str(target)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_sha1_entries_verify_as_git_blobs(tmp_path: Path) -> None:
    """Un activo fijado con el SHA-1 del blob de git debe verificarse como tal.

    Antes se calculaba el SHA-1 crudo y se comparaba contra el OID del blob:
    los 29 activos con hash SHA-1 del manifiesto fallaban todos.
    """
    from comfy_studio.asset_acquire import _verify_integrity, git_blob_sha1

    target = tmp_path / "config.json"
    target.write_bytes(b'{"a": 1}\n')

    assert _verify_integrity(target, _Need(sha1=git_blob_sha1(target))) is True
    bad = _verify_integrity(target, _Need(sha1="0" * 40))
    assert isinstance(bad, str)
    assert "blob de git" in bad
    # El error muestra también el SHA-1 crudo, para que un fijado con la
    # semántica equivocada se diagnostique en vez de decir «no coincide».
    assert "SHA-1 crudo" in bad


def test_sha256_entries_verify_as_raw_content(tmp_path: Path) -> None:
    """El oid LFS sí es el SHA-256 crudo del contenido."""
    import hashlib

    from comfy_studio.asset_acquire import _verify_integrity

    target = tmp_path / "model.safetensors"
    target.write_bytes(b"pesos" * 1000)
    raw = hashlib.sha1(b"pesos" * 1000)  # noqa: F841 - solo para dejar claro que no se usa

    ok = _verify_integrity(target, _Need(sha256=hashlib.sha256(b"pesos" * 1000).hexdigest()))
    assert ok is True
    bad = _verify_integrity(target, _Need(sha256="0" * 64))
    assert isinstance(bad, str) and "LFS" in bad


def test_assets_without_a_hash_are_accepted() -> None:
    """«Where available»: un activo sin hash se acepta."""
    import tempfile

    from comfy_studio.asset_acquire import _verify_integrity

    with tempfile.NamedTemporaryFile(suffix=".bin") as handle:
        assert _verify_integrity(Path(handle.name), _Need()) is True


def test_etag_is_the_git_blob_sha1_not_the_raw_one() -> None:
    """Comprobado contra la API de HuggingFace, no contra una suposición.

    Se descarga UN fichero público pequeño y se comparan las tres cosas: el
    `X-Linked-Etag` que publica HuggingFace, el SHA-1 crudo de sus bytes y el
    OID del blob de git. Si la semántica cambiara en HuggingFace, este test
    falla y hay que revisar el verificador.
    """
    import hashlib
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # noqa: ANN001
            return None

    url = (
        "https://huggingface.co/stabilityai/sd-vae-ft-mse-original/"
        "resolve/main/README.md"
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, method="HEAD"), timeout=30) as resp:
            etag = (resp.headers.get("x-linked-etag") or "").strip().strip('"').lower()
    except urllib.error.HTTPError as exc:
        etag = (exc.headers.get("x-linked-etag") or "").strip().strip('"').lower()
    if not etag:
        pytest.skip("HuggingFace no publicó X-Linked-Etag; no se puede verificar")

    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read()
    raw = hashlib.sha1(body).hexdigest()
    blob = hashlib.sha1(b"blob %d\x00" % len(body) + body).hexdigest()

    assert etag == blob, "el ETag debe ser el OID del blob de git"
    assert etag != raw, "el ETag no es el SHA-1 crudo de los bytes"


# --------------------------------------------------------------------------
# W01 (plan Ultra) — regresiones de los 3 defectos P0 de integridad
# Estas pruebas FALLAN en el código previo al fix y PASAN tras él.
# --------------------------------------------------------------------------


def test_w01_preexisting_corrupt_file_is_not_skipped_as_present(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """P0 #1: un fichero existente con tamaño/hash incorrectos no es «present».

    El skip inicial aceptaba cualquier fichero no vacío. Un parcial de 7 bytes
    frente a 26 esperados se anunciaba como adquirido. Ahora se verifica tamaño
    e integridad antes de hacer skip; si no cuadra, se re-descarga.
    """
    import hashlib

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    need = resolution.missing[0]
    # El manifiesto espera el hash del contenido real del fake-hf.
    need.sha256 = hashlib.sha256(FAKE_CONTENT).hexdigest()
    need.sha1 = None
    need.bytes = len(FAKE_CONTENT)

    # Sembramos un fichero CORRUPTO preexistente en el destino (no vacío, pero
    # con otro contenido y otro tamaño).
    dest = Path(need.dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"corrupto")  # 8 bytes != len(FAKE_CONTENT)

    report = asset_acquire.acquire([need], env=env)
    entry = report.entries[0]
    # No debe quedar como «skipped» (eso sería aceptar el corrupto).
    assert entry.status != "skipped", "un corrupto preexistente no es present"
    # Se re-descargó y ahora el contenido es el correcto, verificado.
    assert entry.status == "downloaded"
    assert dest.read_bytes() == FAKE_CONTENT


def test_w01_download_argv_includes_pinned_revision(
    data_dir: Path, fake_hf: Path, monkeypatch
) -> None:
    """P0 #2: `hf download` recibe --revision cuando el manifiesto lo declara.

    Omitir el pin baja el contenido de `main`, que puede diferir del hash
    fijado. La prueba captura el argv real que se pasa al cliente.
    """
    import hashlib

    env = _env(fake_hf, data_dir)
    resolution = resolve("video.face_restore", env=env)
    need = resolution.missing[0]
    need.sha256 = hashlib.sha256(FAKE_CONTENT).hexdigest()
    need.sha1 = None
    need.revision = "a" * 40  # revisión fijada del manifiesto

    captured: dict[str, list[str]] = {}

    real = asset_acquire._run_hf_download

    def _capture(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = list(argv)
        return real(argv, **kwargs)

    monkeypatch.setattr(asset_acquire, "_run_hf_download", _capture)
    asset_acquire.acquire([need], env=env)

    argv = captured.get("argv", [])
    assert "--revision" in argv, f"el pin no llegó al CLI: {argv}"
    assert argv[argv.index("--revision") + 1] == "a" * 40


def test_w01_exdev_failure_preserves_previous_destination(tmp_path: Path) -> None:
    """P0 #3: si la copia EXDEV falla a mitad, el destino previo sobrevive.

    El fallback antiguo escribía directo sobre `dest`: una copia interrumpida
    dejaba un parcial y destruía la versión válida anterior. Ahora copia a un
    temporal same-fs y hace replace atómico; un fallo no toca `dest`.
    """
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    src.write_bytes(b"nuevo-contenido" * 1000)
    previous = b"version-previa-valida" * 500
    dest.write_bytes(previous)  # destino previo que NO debe perderse

    real_replace = real_os.replace
    calls = {"n": 0}

    def _replace_exdev_then_fail(a, b):  # noqa: ANN001
        # 1ª llamada: el os.replace(src, dest) del happy path -> EXDEV.
        # 2ª llamada: el os.replace(tmp, dest) del fallback -> explota.
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        raise OSError(errno.ENOSPC, "No space left on device")

    real_os.replace = _replace_exdev_then_fail
    try:
        raised = False
        try:
            _move(src, dest)
        except OSError:
            raised = True
    finally:
        real_os.replace = real_replace

    assert raised, "un fallo a mitad de la publicación debe propagarse"
    # El destino previo sigue intacto: no se publicó un parcial.
    assert dest.read_bytes() == previous, "se destruyó/corrompió el destino previo"
    # No quedó ningún temporal en el directorio.
    leftovers = [p for p in dest.parent.iterdir() if p.name.startswith(f".{dest.name}.incoming")]
    assert leftovers == [], f"quedó un temporal sin limpiar: {leftovers}"


def test_w01_exdev_success_is_atomic_and_cleans_up(tmp_path: Path) -> None:
    """La ruta EXDEV feliz publica el contenido nuevo y borra origen/temporal."""
    import errno
    import os as real_os

    from comfy_studio.asset_acquire import _move

    src = tmp_path / "src.bin"
    dest = tmp_path / "dest.bin"
    new_content = b"contenido-nuevo" * 1000
    src.write_bytes(new_content)

    real_replace = real_os.replace

    def _exdev_once_then_real(a, b):  # noqa: ANN001
        # Solo el primer replace(src,dest) finge EXDEV; el replace(tmp,dest) real.
        if Path(a) == src:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(a, b)

    real_os.replace = _exdev_once_then_real
    try:
        _move(src, dest)
    finally:
        real_os.replace = real_replace

    assert dest.read_bytes() == new_content
    assert not src.exists(), "el origen debe desaparecer tras publicar"
    leftovers = [p for p in dest.parent.iterdir() if ".incoming" in p.name]
    assert leftovers == [], "no debe quedar temporal tras una publicación correcta"
