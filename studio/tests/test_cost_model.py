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
