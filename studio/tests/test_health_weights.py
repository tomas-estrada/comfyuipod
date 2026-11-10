"""Pesos del flujo pedido como hecho del disco (no del log de descargas).

- `/studio/api/status` publica un inventario por entrada: qué llegó (con bytes
  reales medidos) y qué falta (`bytes_on_disk: None`, «no disponible»).
- `/studio/api/health` ya no puede fingir que el pod está operativo: los
  activos `bootstrap` ausentes se publican como `weights.required_missing`.
- Ningún campo nuevo escapa a la redacción centralizada.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def data_root(tmp_path, monkeypatch) -> Any:
    """Raíz de datos aprobada (`COMFY_DATA_DIR`) con los pesos del fixture.

    Los destinos del manifiesto se confinan a las raíces aprobadas
    (`model_assets.resolve_dest`): el fixture usa `{COMFY_DATA_DIR}` para no
    depender de una ruta absoluta ni de la plataforma.
    """
    data = tmp_path / "comfyui-data"
    (data / "checkpoints").mkdir(parents=True, exist_ok=True)
    (data / "checkpoints" / "limpio.safetensors").write_bytes(b"x" * 4096)
    monkeypatch.setenv("COMFY_DATA_DIR", str(data))
    return data


@pytest.fixture()
def manifest_fixture(data_root, tmp_path, monkeypatch) -> dict[str, Any]:
    """Manifiesto con dos entradas: una presente (bytes reales) y una ausente."""
    manifest = {
        "version": 1,
        "meta": {"default_entries": 2, "default_bytes": 0},
        "models": [
            {
                "filename": "limpio.safetensors",
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/limpio.safetensors",
                "class": "task",
                "bytes": 4096,
            },
            {
                "filename": "que-no-llego.safetensors",
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/que-no-llego.safetensors",
                "class": "task",
                "bytes": 8192,
            },
        ],
    }
    path = tmp_path / "models-fixture.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("STUDIO_MANIFEST", str(path))
    return manifest


def test_inventario_publica_presentes_con_bytes_reales_y_faltantes_sin_bytes(
    client: TestClient, manifest_fixture
) -> None:
    resp = client.get("/studio/api/status")
    assert resp.status_code == 200, resp.text
    inv = resp.json()["model_assets"]["inventory"]
    assert len(inv) == 2, inv
    presente = next(e for e in inv if e["name"] == "limpio.safetensors")
    assert presente["present"] is True
    # Bytes reales en disco, no los declarados por el manifiesto si difieren.
    assert presente["bytes_on_disk"] == 4096
    ausente = next(e for e in inv if e["name"] == "que-no-llego.safetensors")
    assert ausente["present"] is False
    # Sin medición no se inventa un tamaño: es «no disponible».
    assert ausente["bytes_on_disk"] is None


def test_health_publica_el_estado_de_pesos_requeridos(client: TestClient, manifest_fixture) -> None:
    resp = client.get("/studio/api/health")
    assert resp.status_code == 200, resp.text
    weights = resp.json()["weights"]
    # Sin entradas `bootstrap` en el manifiesto: nada requerido está ausente.
    assert weights["required_ok"] is True
    assert weights["required_missing"] == []


def test_health_reporta_no_disponible_sin_manifiesto(client: TestClient, tmp_path) -> None:
    # Manifiesto sin entradas: no hay medición que mostrar, y no se puede
    # afirmar que los pesos requeridos están («no disponible»).
    path = tmp_path / "models-vacio.json"
    path.write_text(json.dumps({"version": 1, "models": []}), encoding="utf-8")
    import os

    os.environ["STUDIO_MANIFEST"] = str(path)
    try:
        resp = client.get("/studio/api/health")
    finally:
        os.environ.pop("STUDIO_MANIFEST", None)
    assert resp.status_code == 200, resp.text
    weights = resp.json()["weights"]
    assert weights["required_ok"] is None
    assert weights["required_missing"] == []


def test_campos_nuevos_salen_redactados(
    client: TestClient, data_root, tmp_path, monkeypatch
) -> None:
    """Un secreto en el nombre/ruta del activo no puede atravesar la API."""
    token = "hf_ABCDEFGHIJKLMNOP12"
    missing_file = data_root / "checkpoints" / f"falta-hf-{token}.safetensors"
    manifest = {
        "version": 1,
        "models": [
            {
                "filename": missing_file.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/" + missing_file.name,
                "class": "bootstrap",
                "bytes": 128,
            },
        ],
    }
    path = tmp_path / "models-secreto.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("STUDIO_MANIFEST", str(path))

    # /health: el nombre del activo `bootstrap` ausente llega redactado.
    health = client.get("/studio/api/health")
    assert health.status_code == 200, health.text
    assert token not in health.text, "secreto filtrado por health"
    assert "[REDACTED]" in health.text
    # /status: el inventario (dest_path incluido) llega redactado.
    status = client.get("/studio/api/status")
    assert status.status_code == 200, status.text
    assert token not in status.text, "secreto filtrado por status"
    assert "[REDACTED]" in status.text
