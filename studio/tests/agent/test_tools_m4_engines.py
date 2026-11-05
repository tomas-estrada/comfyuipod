"""Tests de las herramientas de auto-extensión del agente (M4): engines y modelos."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from comfy_studio.agent.tools import (
    ToolContext,
    _acquisition_cancel_hook,
    create_default_tool_belt,
)
from comfy_studio.asset_acquire import AcquisitionReport
from comfy_studio.comfyui_client import ComfyUIClient
from comfy_studio.config import StudioConfig
from comfy_studio.progress import ProgressBroker
from comfy_studio.projects import ProjectStore


@pytest.fixture()
def engine_tool_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> ToolContext:
    monkeypatch.delenv("STUDIO_DYNAMIC", raising=False)
    cfg = StudioConfig.from_env()
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    broker = ProgressBroker()
    project = store.create_project(name="Proyecto Engines M4")
    comfy_client = ComfyUIClient(
        base_url=cfg.studio_comfyui_url,
        transport=None,
        store=store,
        broker=broker,
        redactor=cfg.redactor(),
    )
    return ToolContext(
        project_id=project["id"],
        store=store,
        comfyui_client=comfy_client,
        broker=broker,
        config=cfg,
    )


@pytest.mark.anyio
async def test_list_engines_returns_roster(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute("list_engines", {}, engine_tool_context)
    assert result["status"] == "ok"
    engines = result["engines"]
    assert engines["count"] >= 10
    names = {e["name"] for e in engines["engines"]}
    assert {"comfyui", "blender", "editor", "ffmpeg"}.issubset(names)
    # Pod determinista por defecto: dynamic_enabled False
    assert result["dynamic_enabled"] is False


@pytest.mark.anyio
async def test_install_engine_denied_without_dynamic(
    engine_tool_context: ToolContext,
) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {"engine_name": "capture3d", "warm_attempted": True, "task_value": 10.0},
        engine_tool_context,
    )
    assert result["status"] == "denied"
    assert result["decision"]["allowed"] is False
    assert any("STUDIO_DYNAMIC" in r for r in result["decision"]["reasons"])


@pytest.mark.anyio
async def test_install_engine_denied_when_engine_already_warm(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {"engine_name": "blender", "warm_attempted": True, "task_value": 10.0},
        engine_tool_context,
    )
    assert result["status"] == "denied"
    assert any("ya está en el roster" in r for r in result["decision"]["reasons"])


@pytest.mark.anyio
async def test_install_engine_allowed_with_dynamic_and_value(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STUDIO_DYNAMIC", "1")
    belt = create_default_tool_belt()
    result = await belt.execute(
        "install_engine",
        {
            "engine_name": "vgen_x",
            "warm_attempted": True,
            "task_value": 10.0,
            "est_idle_cost": 2.0,
            "source": "https://github.com/example/vgen_x",
        },
        engine_tool_context,
    )
    assert result["status"] == "allowed"
    assert result["decision"]["allowed"] is True


@pytest.mark.anyio
async def test_download_model_reports_animate_group(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    result = await belt.execute("download_model", {"model_group": "animate"}, engine_tool_context)
    assert result["model_group"] == "animate"
    # El grupo Animate alimenta dos plantillas: la base y su continuación.
    assert result["templates"] == ["wan_animate", "wan_animate_cont"]
    # El config del test no activa STUDIO_INCLUDE_ANIMATE -> unavailable con instrucciones
    assert result["status"] in ("available", "unavailable")
    if result["status"] == "unavailable":
        assert "STUDIO_INCLUDE_ANIMATE=1" in result["reason"]


@pytest.mark.anyio
async def test_download_model_rejects_unknown_group(engine_tool_context: ToolContext) -> None:
    belt = create_default_tool_belt()
    from comfy_studio.agent.tools import ToolExecutionError

    with pytest.raises(ToolExecutionError, match="Grupo de pesos desconocido"):
        await belt.execute("download_model", {"model_group": "sd15"}, engine_tool_context)


def test_engine_tools_registered_with_safety_flags() -> None:
    belt = create_default_tool_belt()
    list_spec = belt.get("list_engines")
    assert list_spec is not None and list_spec.parallel_safe is True
    install_spec = belt.get("install_engine")
    assert install_spec is not None and install_spec.parallel_safe is False
    dl_spec = belt.get("download_model")
    assert dl_spec is not None and dl_spec.parallel_safe is True


# --------------------------------------------------------------------------
# Cancelación y progreso de la adquisición de pesos (F16)
# --------------------------------------------------------------------------


def test_acquisition_cancel_hook_has_no_signal_without_job_or_callable(
    tmp_path: Path,
) -> None:
    """Sin job ni callable explícita no hay nada que sondear: `acquire` no paga por ello."""
    store = ProjectStore(studio_data_dir=tmp_path / "studio_data")
    assert _acquisition_cancel_hook(store) is None


def test_acquisition_cancel_hook_reads_the_stored_job_status(
    engine_tool_context: ToolContext,
) -> None:
    """La cancelación se deriva del estado persistido del job, no de la petición."""
    store = engine_tool_context.store
    job = store.create_job(kind="render", project_id=engine_tool_context.project_id)
    hook = _acquisition_cancel_hook(store, job_id=job["id"])
    assert hook is not None
    assert hook() is False, "un job en cola no cancela la adquisición"

    store.update_job(job["id"], status="cancelled")
    assert hook() is True, "job_cancel marca 'cancelled' y la adquisición debe abortar"


def test_acquisition_cancel_hook_combines_job_and_request_scoped_callable(
    engine_tool_context: ToolContext,
) -> None:
    """Sin job, el callable explícito del ámbito de la petición también cancela."""
    store = engine_tool_context.store
    job = store.create_job(kind="render", project_id=engine_tool_context.project_id)
    flag = {"cancel": False}

    hook = _acquisition_cancel_hook(store, job_id=job["id"], cancel=lambda: flag["cancel"])
    assert hook is not None
    assert hook() is False
    flag["cancel"] = True
    assert hook() is True, "la cancelación explícita de la petición aborta igual"


def test_acquisition_cancel_hook_ignores_a_job_from_another_project(
    engine_tool_context: ToolContext,
) -> None:
    """R10: un job cancelado de OTRO proyecto no puede abortar esta descarga."""
    store = engine_tool_context.store
    other_project = store.create_project(name="Proyecto Ajeno")
    foreign_job = store.create_job(kind="render", project_id=other_project["id"])
    store.update_job(foreign_job["id"], status="cancelled")

    hook = _acquisition_cancel_hook(
        store,
        project_id=engine_tool_context.project_id,
        job_id=foreign_job["id"],
    )
    assert hook is not None
    assert hook() is False, (
        "el job cancelado pertenece a otro proyecto: no es señal de esta descarga"
    )


def test_acquisition_cancel_hook_still_reads_the_own_project_job(
    engine_tool_context: ToolContext,
) -> None:
    """R10 no relaja la señal legítima: el job propio cancelado sigue abortando."""
    store = engine_tool_context.store
    job = store.create_job(kind="render", project_id=engine_tool_context.project_id)
    hook = _acquisition_cancel_hook(
        store,
        project_id=engine_tool_context.project_id,
        job_id=job["id"],
    )
    assert hook is not None and hook() is False
    store.update_job(job["id"], status="cancelled")
    assert hook() is True


def test_acquisition_cancel_hook_treats_a_missing_job_as_no_signal(
    engine_tool_context: ToolContext,
) -> None:
    """Un job inexistente (o purgado) tampoco aborta la adquisición en curso."""
    hook = _acquisition_cancel_hook(
        engine_tool_context.store,
        project_id=engine_tool_context.project_id,
        job_id="job-inexistente",
    )
    assert hook is not None
    assert hook() is False


def _force_missing_animate_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grupo pedido y ausente: el handler llega hasta `asset_acquire.acquire`."""
    monkeypatch.setattr(StudioConfig, "animate_weights_expected", lambda self: True)
    monkeypatch.setattr(StudioConfig, "animate_weights_available", lambda self, env=None: False)


@pytest.mark.anyio
async def test_download_model_stops_when_the_job_is_cancelled(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un job cancelado aborta la descarga a mitad y lo dice en el payload."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)
    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    engine_tool_context.store.update_job(job["id"], status="cancelled")

    captured: dict[str, Any] = {}

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        captured.update(kwargs)
        hook = kwargs.get("should_cancel")
        assert hook is not None, "la adquisición debe aceptar un hook de cancelación"
        assert hook() is True, "el job cancelado debe abortar la adquisición"
        # `acquire` corta antes de descargar nada más.
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model",
        {"model_group": "animate", "job_id": job["id"]},
        engine_tool_context,
    )
    assert result["acquisition"]["cancelled"] is True
    assert result["downloaded"] == []
    assert result["failed"] == []
    assert "cancelada" in result["reason"]
    assert captured, "la herramienta debe delegar en asset_acquire.acquire"

    # El progreso de cada activo se publica en el canal SSE del proyecto.
    progress = captured["progress"]
    assert callable(progress)
    progress("wan2.2_animate_14B_int8_convrot.safetensors", "downloading:1")
    events = [
        data
        for kind, data in engine_tool_context.broker.recent_events()
        if kind == "asset_download"
    ]
    assert events, "el avance de la descarga debe publicarse"
    assert events[-1]["filename"] == "wan2.2_animate_14B_int8_convrot.safetensors"
    assert events[-1]["stage"] == "downloading:1"
    assert events[-1]["project_id"] == engine_tool_context.project_id
    assert events[-1]["job_id"] == job["id"]


@pytest.mark.anyio
async def test_request_capability_stops_when_the_job_is_cancelled(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`request_capability` también cancela la adquisición con el job cancelado."""
    import comfy_studio.capabilities as capabilities

    class _StubResolution:
        state = "assets_missing"
        engine = "comfyui"
        engine_prep: dict[str, Any] = {"state": "prepared"}
        selected_template = "face_fix"
        summary = "faltan activos"
        missing: list[Any] = [object()]

        def to_public_dict(self) -> dict[str, Any]:
            return {"state": self.state, "summary": self.summary}

    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    engine_tool_context.store.update_job(job["id"], status="cancelled")
    monkeypatch.setattr(capabilities, "resolve", lambda capability: _StubResolution())

    captured: dict[str, Any] = {}

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        captured.update(kwargs)
        hook = kwargs.get("should_cancel")
        assert hook is not None and hook() is True
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    import comfy_studio.asset_acquire as asset_acquire

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "request_capability",
        {"capability": "video.face_restore", "job_id": job["id"]},
        engine_tool_context,
    )
    assert result["acquisition"]["cancelled"] is True
    assert result["downloaded"] == []
    assert captured["progress"] is not None
    assert captured, "la herramienta debe delegar en asset_acquire.acquire"


# --------------------------------------------------------------------------
# R10: el job_id de la adquisición se ata al proyecto activo
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_download_model_rejects_a_job_id_from_another_project(
    engine_tool_context: ToolContext,
) -> None:
    """R10: `download_model` rechaza un job_id que no es del proyecto activo."""
    from comfy_studio.agent.tools import ToolExecutionError

    other_project = engine_tool_context.store.create_project(name="Proyecto Ajeno")
    foreign_job = engine_tool_context.store.create_job(
        kind="render", project_id=other_project["id"]
    )

    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="proyecto activo"):
        await belt.execute(
            "download_model",
            {"model_group": "animate", "job_id": foreign_job["id"]},
            engine_tool_context,
        )


@pytest.mark.anyio
async def test_request_capability_rejects_a_job_id_from_another_project(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10: `request_capability` rechaza el job ajeno antes de adquirir nada."""
    import comfy_studio.capabilities as capabilities
    from comfy_studio.agent.tools import ToolExecutionError

    class _StubResolution:
        state = "assets_missing"
        engine = "comfyui"
        engine_prep: dict[str, Any] = {"state": "prepared"}
        selected_template = "face_fix"
        summary = "faltan activos"
        missing: list[Any] = [object()]

        def to_public_dict(self) -> dict[str, Any]:
            return {"state": self.state, "summary": self.summary}

    monkeypatch.setattr(capabilities, "resolve", lambda capability: _StubResolution())
    other_project = engine_tool_context.store.create_project(name="Proyecto Ajeno")
    foreign_job = engine_tool_context.store.create_job(
        kind="render", project_id=other_project["id"]
    )

    def _acquire_must_not_run(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        raise AssertionError("un job_id de otro proyecto no debe llegar a adquirir")

    import comfy_studio.asset_acquire as asset_acquire

    monkeypatch.setattr(asset_acquire, "acquire", _acquire_must_not_run)

    belt = create_default_tool_belt()
    with pytest.raises(ToolExecutionError, match="proyecto activo"):
        await belt.execute(
            "request_capability",
            {"capability": "video.face_restore", "job_id": foreign_job["id"]},
            engine_tool_context,
        )


@pytest.mark.anyio
async def test_download_model_accepts_the_active_project_job(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R10 no rompe el caso legítimo: el job propio se acepta y se propaga."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)
    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )

    captured: dict[str, Any] = {}

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        captured.update(kwargs)
        return AcquisitionReport(hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model",
        {"model_group": "animate", "job_id": job["id"]},
        engine_tool_context,
    )
    assert result["job_id"] == job["id"]
    assert captured["should_cancel"] is not None
    assert captured["should_cancel"]() is False


# --------------------------------------------------------------------------
# R11: la cancelación del ámbito de la petición es opt-in del embebedor
# --------------------------------------------------------------------------


def test_request_scoped_cancel_is_opt_in_for_embedders(
    engine_tool_context: ToolContext,
) -> None:
    """R11: el contexto de producción no trae `cancel_requested` (camino del job)."""
    from comfy_studio.agent.tools import _request_cancel_hook

    assert _request_cancel_hook(engine_tool_context) is None
    assert engine_tool_context.cancel_requested is None
    # Sin job ni callable no hay nada que sondear: `acquire` no paga por ello.
    assert _acquisition_cancel_hook(engine_tool_context.store) is None


@pytest.mark.anyio
async def test_download_model_honours_an_embedder_supplied_cancel_callable(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R11: quien embebe inyecta `cancel_requested` y el hook lo respeta."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)
    flag = {"cancel": False}
    context = ToolContext(
        project_id=engine_tool_context.project_id,
        store=engine_tool_context.store,
        comfyui_client=engine_tool_context.comfyui_client,
        broker=engine_tool_context.broker,
        config=engine_tool_context.config,
        cancel_requested=lambda: flag["cancel"],
    )

    captured: dict[str, Any] = {}

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        captured.update(kwargs)
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model", {"model_group": "animate"}, context
    )
    assert result["acquisition"]["cancelled"] is True
    hook = captured["should_cancel"]
    assert hook is not None, "sin job la señal viene del callable de la petición"
    assert hook() is False, "mientras el embebedor no pide cancelar, se descarga"
    flag["cancel"] = True
    assert hook() is True, "el callable inyectado por el embebedor aborta la descarga"


# --------------------------------------------------------------------------
# R27: la adquisición corre fuera del event loop
# --------------------------------------------------------------------------


def _fake_blocking_hf_cli(tmp_path: Path) -> Path:
    """Cliente `hf` de mentira que se queda colgado: una «descarga» larga."""
    script = tmp_path / "hf_falso.sh"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.mark.anyio
async def test_download_model_cancels_mid_flight_with_the_real_blocking_acquire(
    engine_tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R27: un `job_cancel` concurrente aborta la descarga EN CURSO.

    Se usa el `asset_acquire.acquire` REAL, con su `proc.wait(timeout=1.0)` que
    bloquea, y un cliente `hf` que se queda colgado. Si la adquisición se
    awaiteara dentro del event loop, el `update_job` de abajo no podría ejecutarse
    hasta que la descarga hubiera terminado: `should_cancel` nunca vería el
    'cancelled' y el intento moriría por timeout. Este test falla (por timeout,
    no por cancelación) si la adquisición vuelve al loop.
    """
    import comfy_studio.capabilities as capabilities
    from comfy_studio.capabilities import AssetNeed

    _force_missing_animate_group(monkeypatch)
    script = _fake_blocking_hf_cli(tmp_path)
    dest = tmp_path / "models" / "peso_falso.safetensors"
    monkeypatch.setattr(
        capabilities,
        "needs_for_group",
        lambda flag, env=None: [
            AssetNeed(
                filename="peso_falso.safetensors",
                dest_path=str(dest),
                bytes=1024,
                asset_class="diffusion_models",
                present=False,
                source="studio",
                repo_id="org/repo",
                remote_path="peso_falso.safetensors",
            )
        ],
    )
    monkeypatch.setenv("HF_CLI", str(script))
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path / "comfy-data"))
    # Timeout corto: sin cancelación a mitad el intento muere por timeout y el
    # test falla en segundos en lugar de colgarse un minuto.
    monkeypatch.setenv("HF_FETCH_TIMEOUT", "3")
    monkeypatch.setenv("HF_DISK_MARGIN_BYTES", "0")

    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    belt = create_default_tool_belt()

    started = time.monotonic()
    task = asyncio.create_task(
        belt.execute(
            "download_model",
            {"model_group": "animate", "job_id": job["id"]},
            engine_tool_context,
        )
    )
    # La descarga ya está en marcha en su propio hilo: el loop sigue libre, así
    # que este sleep y el «job_cancel» equivalente pueden correr ahora.
    await asyncio.sleep(0.4)
    engine_tool_context.store.update_job(job["id"], status="cancelled")
    result = await asyncio.wait_for(task, timeout=30)
    elapsed = time.monotonic() - started

    assert result["acquisition"]["cancelled"] is True
    statuses = {e["status"] for e in result["acquisition"]["entries"]}
    assert "cancelled" in statuses, "la entrada debe quedar cancelada, no fallida"
    # Abortó por la cancelación y no por el timeout del cliente (3 s).
    assert elapsed < 2.9, f"la cancelación tardó {elapsed:.1f}s: no fue a mitad"
    assert result["reason"] == "Descarga cancelada: el job asociado fue cancelado."
    # El progreso se publica desde el hilo de descarga: tiene que llegar igual.
    stages = [
        data["stage"]
        for kind, data in engine_tool_context.broker.recent_events()
        if kind == "asset_download"
    ]
    assert "downloading:1" in stages
    assert "cancelled" in stages


@pytest.mark.anyio
async def test_request_capability_cancels_mid_flight_with_the_real_blocking_acquire(
    engine_tool_context: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """R27: `request_capability` cancela a mitad con el `acquire` real también."""
    import comfy_studio.capabilities as capabilities
    from comfy_studio.capabilities import AssetNeed

    class _StubResolution:
        state = "assets_missing"
        engine = "comfyui"
        engine_prep: dict[str, Any] = {"state": "prepared"}
        selected_template = "face_fix"
        summary = "faltan activos"
        missing: list[Any] = [
            AssetNeed(
                filename="peso_falso.safetensors",
                dest_path=str(tmp_path / "models" / "peso_falso.safetensors"),
                bytes=1024,
                asset_class="diffusion_models",
                present=False,
                source="studio",
                repo_id="org/repo",
                remote_path="peso_falso.safetensors",
            )
        ]

        def to_public_dict(self) -> dict[str, Any]:
            return {"state": self.state, "summary": self.summary}

    monkeypatch.setattr(capabilities, "resolve", lambda capability: _StubResolution())
    script = _fake_blocking_hf_cli(tmp_path)
    monkeypatch.setenv("HF_CLI", str(script))
    monkeypatch.setenv("COMFY_DATA_DIR", str(tmp_path / "comfy-data"))
    monkeypatch.setenv("HF_FETCH_TIMEOUT", "3")
    monkeypatch.setenv("HF_DISK_MARGIN_BYTES", "0")

    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    belt = create_default_tool_belt()

    started = time.monotonic()
    task = asyncio.create_task(
        belt.execute(
            "request_capability",
            {"capability": "video.face_restore", "job_id": job["id"]},
            engine_tool_context,
        )
    )
    await asyncio.sleep(0.4)
    engine_tool_context.store.update_job(job["id"], status="cancelled")
    result = await asyncio.wait_for(task, timeout=30)
    elapsed = time.monotonic() - started

    assert result["acquisition"]["cancelled"] is True
    assert result["cancelled"] is True
    assert elapsed < 2.9, f"la cancelación tardó {elapsed:.1f}s: no fue a mitad"
    assert "cancelada" in result["reason"]
    assert "cancelada" in result["next_step"]


# --------------------------------------------------------------------------
# R28: el motivo de la cancelación nombra la señal que la pidió
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_download_model_cancel_reason_names_the_job_source(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28: cancelado por el job persistido, el motivo dice «el job asociado»."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)
    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    engine_tool_context.store.update_job(job["id"], status="cancelled")

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        assert kwargs["should_cancel"]() is True
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model",
        {"model_group": "animate", "job_id": job["id"]},
        engine_tool_context,
    )
    assert result["reason"] == "Descarga cancelada: el job asociado fue cancelado."


@pytest.mark.anyio
async def test_download_model_cancel_reason_names_the_request_source(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28: cancelado por el callable de la petición, el motivo NO culpa al job."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)
    # Job vivo (nada cancelado) y señal explícita de la petición: antes se
    # atribuía al job una cancelación que él no pidió.
    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    flag = {"cancel": True}
    context = ToolContext(
        project_id=engine_tool_context.project_id,
        store=engine_tool_context.store,
        comfyui_client=engine_tool_context.comfyui_client,
        broker=engine_tool_context.broker,
        config=engine_tool_context.config,
        cancel_requested=lambda: flag["cancel"],
    )

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        assert kwargs["should_cancel"]() is True
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model",
        {"model_group": "animate", "job_id": job["id"]},
        context,
    )
    assert result["reason"] == "Descarga cancelada: lo pidió quien abrió la petición."
    assert engine_tool_context.store.get_job(job["id"])["status"] != "cancelled"


@pytest.mark.anyio
async def test_download_model_cancel_reason_is_neutral_when_the_source_is_unknown(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28: sin origen determinable se dice lo cierto y no se inventa un culpable."""
    import comfy_studio.asset_acquire as asset_acquire

    _force_missing_animate_group(monkeypatch)

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        # Un `acquire` que cancela sin pasar por nuestro hook (otro embebedor).
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    belt = create_default_tool_belt()
    result = await belt.execute(
        "download_model", {"model_group": "animate"}, engine_tool_context
    )
    assert "cancelada" in result["reason"]
    assert "job" not in result["reason"]


# --------------------------------------------------------------------------
# R29: `request_capability` distingue cancelación de fallo
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_request_capability_reports_a_cancelled_acquisition_as_such(
    engine_tool_context: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R29: cancelada NO es «faltan activos y no se pudieron adquirir»."""
    import comfy_studio.asset_acquire as asset_acquire
    import comfy_studio.capabilities as capabilities

    class _StubResolution:
        state = "assets_missing"
        engine = "comfyui"
        engine_prep: dict[str, Any] = {"state": "prepared"}
        selected_template = "face_fix"
        summary = "faltan activos"
        missing: list[Any] = [object()]

        def to_public_dict(self) -> dict[str, Any]:
            return {"state": self.state, "summary": self.summary}

    monkeypatch.setattr(capabilities, "resolve", lambda capability: _StubResolution())

    def _fake_acquire(needs: list[Any], **kwargs: Any) -> AcquisitionReport:
        assert kwargs["should_cancel"]() is True
        return AcquisitionReport(cancelled=True, hf_cli="hf", stage_dir="/stage")

    monkeypatch.setattr(asset_acquire, "acquire", _fake_acquire)

    job = engine_tool_context.store.create_job(
        kind="render", project_id=engine_tool_context.project_id
    )
    engine_tool_context.store.update_job(job["id"], status="cancelled")

    belt = create_default_tool_belt()
    result = await belt.execute(
        "request_capability",
        {"capability": "video.face_restore", "job_id": job["id"]},
        engine_tool_context,
    )
    assert result["acquisition"]["cancelled"] is True
    assert result["cancelled"] is True
    assert result["reason"] == "Descarga cancelada: el job asociado fue cancelado."
    assert "cancelada" in result["next_step"]
    assert "no se pudieron adquirir" not in result["next_step"]
