"""Pesos del flujo pedido como hecho del disco (no del log de descargas).

- `/studio/api/status` publica un inventario por entrada: qué llegó (con bytes
  reales medidos) y qué falta (`bytes_on_disk: None`, «no disponible»).
- `/studio/api/health` ya no puede fingir que el pod está operativo: los
  activos `bootstrap` ausentes se publican como `weights.required_missing`.
- Ningún campo nuevo escapa a la redacción centralizada.
"""

from __future__ import annotations

import hashlib
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


def test_inventario_distingue_integridad_y_stage_para_agente_y_health(
    client: TestClient, data_root, tmp_path, monkeypatch
) -> None:
    """La presencia en disco no demuestra que el modelo sea íntegro."""
    verified_size = data_root / "checkpoints" / "size-verified.bin"
    verified_size.write_bytes(b"four")
    verified_hash = data_root / "checkpoints" / "hash-verified.bin"
    verified_hash.write_bytes(b"hash fixture")
    expected_hash = hashlib.sha256(verified_hash.read_bytes()).hexdigest()
    from comfy_studio.asset_acquire import _record_integrity

    _record_integrity(verified_hash, expected_hash, "sha256")
    verified_wrong_digest = data_root / "checkpoints" / "wrong-digest.bin"
    verified_wrong_digest.write_bytes(b"wrong but verified")
    actual_wrong_digest = hashlib.sha256(verified_wrong_digest.read_bytes()).hexdigest()
    _record_integrity(verified_wrong_digest, actual_wrong_digest, "sha256")
    unverified = data_root / "checkpoints" / "unverified.bin"
    unverified.write_bytes(b"different payload")
    corrupt = data_root / "checkpoints" / "wrong-size.bin"
    corrupt.write_bytes(b"bad")

    stage_root = data_root / "models" / ".hf_stage_capability" / "job-123"
    partial = stage_root / "checkpoints" / "staged.bin.incomplete"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    manifest = {
        "version": 1,
        "models": [
            {
                "filename": verified_size.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/size-verified.bin",
                "bytes": 4,
            },
            {
                "filename": verified_hash.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/hash-verified.bin",
                "sha256": expected_hash,
            },
            {
                "filename": verified_wrong_digest.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/wrong-digest.bin",
                "sha256": "0" * 64,
            },
            {
                "filename": unverified.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/unverified.bin",
                "sha256": "0" * 64,
            },
            {
                "filename": corrupt.name,
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/wrong-size.bin",
                "bytes": 8,
            },
            {
                "filename": "staged.bin",
                "remote_path": "checkpoints/staged.bin",
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/staged.bin",
                "bytes": 7,
            },
            {
                "filename": "missing.bin",
                "dest_path": "{COMFY_DATA_DIR}/checkpoints/missing.bin",
                "bytes": 7,
            },
        ],
    }
    path = tmp_path / "integrity-fixture.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("STUDIO_MANIFEST", str(path))

    status = client.get("/studio/api/status")
    assert status.status_code == 200, status.text
    inventory = {row["name"]: row for row in status.json()["model_assets"]["inventory"]}
    assert inventory[verified_size.name]["integrity_state"] == "verified"
    assert inventory[verified_hash.name]["integrity_state"] == "verified"
    assert inventory[verified_wrong_digest.name]["integrity_state"] == "corrupt"
    assert inventory[unverified.name]["integrity_state"] == "present_unverified"
    assert inventory[corrupt.name]["integrity_state"] == "corrupt"
    assert inventory["staged.bin"]["integrity_state"] == "staging"
    assert inventory["missing.bin"]["integrity_state"] == "missing"

    health = client.get("/studio/api/health")
    assert health.status_code == 200, health.text
    assert health.json()["asset_integrity"] == {
        "verified": 2,
        "present_unverified": 1,
        "corrupt": 2,
        "staging": 1,
        "missing": 1,
        "unavailable": 0,
    }


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
