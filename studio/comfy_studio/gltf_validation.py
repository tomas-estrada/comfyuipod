"""Offline Khronos glTF validation for project-owned GLB assets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .assets3d import Asset3DError

VALIDATOR_VERSION = "2.0.0-dev.3.10"
MAX_GLTF_ASSET_BYTES = 512 * 1024 * 1024
MAX_VALIDATION_REPORT_BYTES = 256 * 1024
VALIDATOR_TIMEOUT_SECONDS = 30
MAX_VALIDATION_ISSUES = 50
_VALIDATOR_DIR_ENV = "STUDIO_GLTF_VALIDATOR_DIR"


def _validator_dir() -> Path:
    configured = os.environ.get(_VALIDATOR_DIR_ENV)
    candidates = [
        Path(configured) if configured else None,
        Path("/opt/gltf-validator"),
        Path(__file__).resolve().parents[1] / "gltf-validator",
    ]
    for candidate in candidates:
        if candidate and (candidate / "validate.cjs").is_file():
            return candidate.resolve()
    raise Asset3DError("Khronos glTF-Validator no está instalado en esta imagen.")


def validate_glb(
    path: Path,
    *,
    project_root: Path,
    asset_sha256: str,
) -> dict[str, Any]:
    """Validate one owned GLB and return a deterministic, bounded issue report."""
    try:
        root = project_root.resolve(strict=True)
        asset_path = path.resolve(strict=True)
    except OSError as exc:
        raise Asset3DError("el GLB del proyecto no está disponible para validación") from exc
    if not asset_path.is_relative_to(root) or not asset_path.is_file():
        raise Asset3DError("el GLB de validación debe estar dentro del proyecto")
    if asset_path.suffix.lower() != ".glb":
        raise Asset3DError("Khronos glTF-Validator solo se ejecuta sobre GLB")
    try:
        if not asset_path.is_file():
            raise Asset3DError("el GLB de validación debe ser un archivo regular")
        size = asset_path.stat().st_size
    except OSError as exc:
        raise Asset3DError("no se pudo leer el GLB del proyecto") from exc
    if not 20 <= size <= MAX_GLTF_ASSET_BYTES:
        raise Asset3DError("GLB excede el tamaño validable de 512 MiB")
    if len(asset_sha256) != 64 or any(char not in "0123456789abcdef" for char in asset_sha256):
        raise Asset3DError("SHA-256 del GLB no es válido")

    validator_dir = _validator_dir()
    script = validator_dir / "validate.cjs"
    if not (validator_dir / "node_modules" / "gltf-validator").is_dir():
        raise Asset3DError("Khronos glTF-Validator no está instalado en esta imagen.")
    node = shutil.which("node")
    if not node:
        raise Asset3DError("Node.js 22 no está disponible para validar el GLB.")
    try:
        result = subprocess.run(
            [node, str(script), str(asset_path)],
            cwd=validator_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VALIDATOR_TIMEOUT_SECONDS,
            check=False,
            env={
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise Asset3DError("Khronos glTF-Validator excedió el límite de 30 segundos.") from exc
    except OSError as exc:
        raise Asset3DError("no se pudo iniciar Khronos glTF-Validator.") from exc
    try:
        validated_sha256 = _sha256_path(asset_path)
    except OSError as exc:
        raise Asset3DError("el GLB cambió durante la validación Khronos") from exc
    if validated_sha256 != asset_sha256:
        raise Asset3DError("el GLB cambió durante la validación Khronos")
    if len(result.stdout) > MAX_VALIDATION_REPORT_BYTES:
        raise Asset3DError("el informe glTF supera el límite de 256 KiB")
    try:
        report = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Asset3DError("Khronos glTF-Validator no produjo un informe JSON válido.") from exc
    if not isinstance(report, dict) or report.get("validator") != "Khronos glTF-Validator":
        raise Asset3DError("el informe no identifica al validador Khronos esperado")
    if report.get("version") != VALIDATOR_VERSION:
        raise Asset3DError("la versión de Khronos glTF-Validator no coincide con el pin")
    count_fields = ("num_errors", "num_warnings", "num_infos", "num_hints")
    if any(
        type(report.get(field)) is not int or report[field] < 0
        for field in count_fields
    ):
        raise Asset3DError("el informe Khronos contiene contadores inválidos")
    messages = report.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_VALIDATION_ISSUES:
        raise Asset3DError("el informe Khronos contiene demasiados problemas")
    if result.returncode not in (0, 1):
        raise Asset3DError("Khronos glTF-Validator no pudo analizar el GLB")
    if (result.returncode == 1) != (report["num_errors"] > 0):
        raise Asset3DError("estado del proceso y errores del informe Khronos no coinciden")
    if report["num_errors"] > 0:
        raise Asset3DError(
            f"Khronos glTF-Validator encontró {report['num_errors']} errores en el GLB."
        )
    return {**report, "asset_sha256": asset_sha256}


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["VALIDATOR_VERSION", "validate_glb"]
