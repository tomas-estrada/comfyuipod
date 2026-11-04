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


@pytest.fixture()
def fake_hf(tmp_path: Path) -> Path:
    """Cliente `hf` de mentira: crea el archivo remoto bajo --local-dir."""
    script = tmp_path / "fake-hf"
    script.write_text(
        "#!/bin/bash\n"
        'mkdir -p "$(dirname "$7/$3")"\n'
        f'printf "{FAKE_CONTENT.decode()}" > "$7/$3"\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


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


def test_bootstrap_assets_are_the_small_set(data_dir: Path) -> None:
    """La política prohíbe llamar bootstrap a una colección grande."""
    resolution = resolve("video.face_restore", env={"COMFY_DATA_DIR": str(data_dir)})
    bootstrap = [a for a in resolution.assets if a.asset_class == "bootstrap"]
    assert bootstrap, "el stack facial/VAE debe ser bootstrap"
    assert sum(a.bytes for a in bootstrap) < 5 * 1024**3


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


def test_acquire_without_hf_client_fails_closed(data_dir: Path) -> None:
    env = {"COMFY_DATA_DIR": str(data_dir), "PATH": "/nonexistent-bin"}
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
    script = tmp_path / "fake-hf"
    script.write_text(
        "#!/bin/bash\nmkdir -p \"$(dirname \"$7/$3\")\"; printf pesos > \"$7/$3\"\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
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
