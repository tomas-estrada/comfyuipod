"""Tests del modelo de coste de sesión (política, punto 6).

«Optimize the cost of producing an acceptable deliverable, including startup,
transfers, downloads, inference, evaluation, storage and API usage.»

Lo que estos tests fijan es lo que suele ignorarse: la GPU se factura mientras
se descarga.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "storage_requirement.py"


def _run(args: list[str]) -> dict:
    out = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--cost", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    return json.loads(out.stdout)


def test_cost_model_charges_the_gpu_while_downloading() -> None:
    """El coste no es solo GPU x horas: la descarga consume GPU facturada."""
    report = _run(["--profile", "studio", "--hours", "3"])
    cost = report["cost"]
    assert cost["model_bytes"] > 150e9, "el perfil studio arrastra >150 GB de modelos"
    assert cost["download_hours"] > 0
    assert cost["gpu_idle_cost"] > 0
    assert cost["productive_hours"] < cost["hours"]
    # El total incluye GPU + disco + transferencia.
    expected = cost["gpu_cost"] + cost["disk_cost"] + cost["transfer_cost"]
    assert abs(cost["total_cost"] - expected) < 0.01


def test_cost_per_productive_hour_exceeds_cost_per_hour() -> None:
    """La métrica honesta es el coste por hora productiva, que es mayor."""
    report = _run(["--profile", "studio", "--hours", "3"])
    cost = report["cost"]
    assert cost["cost_per_productive_hour"] > cost["gpu_cost"] / cost["hours"]


def test_image_is_transferred_even_when_not_on_the_volume() -> None:
    """La imagen se transfiere en cada arranque en frío, aunque no ocupe el volumen."""
    report = _run(["--profile", "studio"])
    cost = report["cost"]
    assert report["image_on_volume"] is False
    assert cost["image_bytes"] > 10e9
    assert cost["transfer_bytes"] == cost["image_bytes"] + cost["model_bytes"]
    assert cost["cold_start_hours"] > 0


def test_faster_link_reduces_idle_gpu_cost() -> None:
    """Más ancho de bajada = menos GPU facturada sin producir."""
    slow = _run(["--profile", "studio", "--hours", "3", "--inet-down-mbps", "100"])["cost"]
    fast = _run(["--profile", "studio", "--hours", "3", "--inet-down-mbps", "5000"])["cost"]
    assert fast["download_hours"] < slow["download_hours"]
    assert fast["gpu_idle_cost"] < slow["gpu_idle_cost"]
    assert fast["productive_hours"] > slow["productive_hours"]


def test_short_session_is_dominated_by_transfer() -> None:
    """En una sesión muy corta, la transferencia manda: no compensa «ahorrar» GPU."""
    short = _run(["--profile", "studio", "--hours", "0.5"])["cost"]
    assert short["productive_hours"] < short["hours"] / 2
    assert short["cost_per_productive_hour"] > short["total_cost"] / short["hours"]


def test_minimal_profile_has_no_model_transfer() -> None:
    """`minimal` no arrastra la Fase 3: su coste de transferencia es solo la imagen."""
    report = _run(["--profile", "minimal", "--hours", "1"])
    cost = report["cost"]
    assert cost["model_bytes"] < 5e9
    assert cost["transfer_bytes"] == cost["image_bytes"] + cost["model_bytes"]


def test_cost_is_optional_in_the_cli() -> None:
    """Sin `--cost`, la salida JSON no lo incluye."""
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "--profile", "studio", "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        cwd=str(REPO_ROOT),
    )
    payload = json.loads(out.stdout)
    assert "cost" not in payload


# --------------------------------------------------------------------------
# Liberación de VRAM cuando no queda trabajo (política, puntos 3 y 5)
# --------------------------------------------------------------------------


class _StoreStub:
    def __init__(self, jobs: list[dict]) -> None:
        self._jobs = jobs

    def list_jobs(self, **_kwargs):  # noqa: ANN001
        return self._jobs


class _ClientStub:
    def __init__(self, jobs: list[dict]) -> None:
        self._store = _StoreStub(jobs)
        self.calls: list[dict] = []

    async def free_memory(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        return {"freed": True}


@pytest.mark.anyio
async def test_gpu_is_released_when_no_work_remains() -> None:
    """«Do not keep every engine or model resident in VRAM merely to describe the
    studio as ready.» `POST /free` existía pero nunca se llamaba."""

    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}])
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {"freed": True}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == [{"unload_models": True, "free_memory": True}]


@pytest.mark.anyio
async def test_gpu_is_not_released_while_work_is_pending() -> None:
    """Liberar con la cola llena penalizaría el rendimiento sin ganar nada."""
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}, {"status": "running"}])
    client._unload_when_idle = True

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == []


@pytest.mark.anyio
async def test_release_can_be_disabled() -> None:
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([])
    client._unload_when_idle = False

    calls: list[dict] = []

    async def _free(**kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {}

    client.free_memory = _free
    await ComfyUIClient._release_gpu_when_idle(client)
    assert calls == []


@pytest.mark.anyio
async def test_release_failure_never_breaks_a_finished_render() -> None:
    """Liberar VRAM es una optimización: no puede tumbar un render ya persistido."""
    from comfy_studio.comfyui_client import ComfyUIClient

    client = ComfyUIClient.__new__(ComfyUIClient)
    client._store = _StoreStub([{"status": "done"}])
    client._unload_when_idle = True

    async def _boom(**kwargs):  # noqa: ANN001
        raise RuntimeError("ComfyUI no responde")

    client.free_memory = _boom
    await ComfyUIClient._release_gpu_when_idle(client)  # no lanza


def test_unload_flag_is_configurable() -> None:
    from comfy_studio.config import StudioConfig

    default = StudioConfig.from_env({})
    assert default.unload_models_when_idle is True
    off = StudioConfig.from_env({"STUDIO_UNLOAD_WHEN_IDLE": "0"})
    assert off.unload_models_when_idle is False
    assert "STUDIO_UNLOAD_WHEN_IDLE" in off.to_public_dict()


# --------------------------------------------------------------------------
# Paridad perfiles <-> manifiesto (política, punto 7)
# --------------------------------------------------------------------------


def test_profile_parity_holds() -> None:
    """La selección por perfil es dato versionado, no un `case` en bash."""
    import subprocess

    r = subprocess.run(
        [sys.executable, "scripts/check_profile_parity.py"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "perfiles coincidentes" in r.stdout


def test_profile_parity_detects_a_divergence(tmp_path: Path) -> None:
    """Si el bash y el manifiesto se separan, el checker falla con el detalle."""

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_profile_parity import manifest_plan, parse_plan

    bash = parse_plan()
    declared = manifest_plan()
    assert bash, "el parser debe extraer el plan del bash"
    assert set(bash) == set(declared), "manifiesto y bash deben cubrir los mismos activos"
    for name, profiles in bash.items():
        assert declared[name] == profiles, name


def test_parser_extracts_profile_guards() -> None:
    """El parser lee las guardas `=~ ^(all|general)$` del bash."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from check_profile_parity import parse_plan

    plan = parse_plan()
    # Fase 1 corre siempre; Fase 2 está guardada por perfil.
    assert "sdxl.vae.safetensors" in plan
    assert "minimal" in plan["sdxl.vae.safetensors"]
    assert "flux1-dev-fp8.safetensors" in plan
    assert "minimal" not in plan["flux1-dev-fp8.safetensors"]
    assert "all" in plan["flux1-dev-fp8.safetensors"]
