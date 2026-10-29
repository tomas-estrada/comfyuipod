"""Cliente del worker de ML en subprocesso (M3.1).

`run_worker(subcommand, payload, timeout_s)` lanza `worker.py` como proceso
hijo con `subprocess.run(capture_output=True, timeout=...)`, nunca `shell=True`.
Mapea códigos de salida a errores tipados; toda falla lleva el tail de stderr
redactado. Los timeouts son configurables por `STUDIO_ML_TIMEOUT_S` con valores
por defecto por subcomando (30s probe, 120s embeddings/faces/diarize,
300s asr_transcribe).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from comfy_studio.config import SecretRedactor
from comfy_studio.ml import (
    ML_DEFAULT_TIMEOUTS,
    ML_ENV_ALLOWLIST,
    MlWorkerResponse,
    probe_capabilities,
)


class MlWorkerError(RuntimeError):
    """El worker salió con rc != 0 o produjo un error tipado."""

    def __init__(
        self,
        message: str,
        *,
        subcommand: str | None = None,
        exit_code: int | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.subcommand = subcommand
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class MlWorkerTimeout(MlWorkerError):
    """El worker excedió el timeout."""


def _default_timeout(subcommand: str, override: float | None) -> float:
    if override is not None and override > 0:
        return override
    return ML_DEFAULT_TIMEOUTS.get(subcommand, 120.0)


def _build_env() -> dict[str, str] | None:
    """Entorno del hijo: allowlist estricta, sin secretos."""
    env: dict[str, str] = {}
    for key in sorted(ML_ENV_ALLOWLIST):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env or None


def run_worker(
    subcommand: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    redactor: SecretRedactor | None = None,
) -> MlWorkerResponse:
    """Ejecuta `worker.py <subcommand>` con `payload` como stdin JSON.

    Devuelve `MlWorkerResponse` con `ok=True` y `payload` en éxito, o
    `ok=False` con `error`/`stderr_tail`/`exit_code` en fallo. Nunca lanza
    excepción por fallo del worker: todo se tipifica en la respuesta.
    """
    redactor = redactor or SecretRedactor()
    payload = payload or {}
    timeout = _default_timeout(subcommand, timeout_s)
    worker_path = str(Path(__file__).resolve().parent / "worker.py")
    interpreter = os.environ.get("STUDIO_ML_PYTHON_BIN") or sys.executable

    env = _build_env()
    stdin_text = json.dumps(payload, ensure_ascii=False)

    start = time.monotonic()
    try:
        res = subprocess.run(
            [interpreter, worker_path, subcommand],
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.monotonic() - start
        tail = redactor.redact(
            exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        )
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"worker {subcommand} timeout tras {elapsed:.1f}s (límite {timeout:.0f}s)",
            exit_code=None,
            stderr_tail=tail,
        )
    except Exception as exc:  # noqa: BLE001
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"{type(exc).__name__}: {exc}",
            exit_code=None,
            stderr_tail=redactor.redact(str(exc)),
        )

    # Parsear stdout JSON
    try:
        parsed = json.loads(res.stdout) if res.stdout.strip() else {}
    except Exception as exc:  # noqa: BLE001
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"salida del worker no es JSON válido: {exc}",
            exit_code=res.returncode,
            stderr_tail=redactor.redact(res.stderr),
        )

    if res.returncode != 0:
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=parsed.get("error")
            or f"worker {subcommand} salió con rc={res.returncode}",
            exit_code=res.returncode,
            stderr_tail=parsed.get("stderr_tail") or redactor.redact(res.stderr),
            payload=parsed.get("payload"),
        )

    return MlWorkerResponse(
        ok=True,
        subcommand=subcommand,
        payload=parsed.get("payload", {}),
        exit_code=res.returncode,
        stderr_tail=parsed.get("stderr_tail"),
    )


def run_worker_with_probe_fallback(
    subcommand: str,
    payload: dict[str, Any] | None = None,
    timeout_s: float | None = None,
    require_real: bool = False,
    redactor: SecretRedactor | None = None,
) -> MlWorkerResponse:
    """Como `run_worker` pero, si `require_real` es False y el worker falla
    por intérprete/pesos ausentes, devuelve una respuesta simulada en vez de
    error (modo `sim`). Si `require_real` es True, el error se propaga
    (fail-closed, modo `real`).
    """
    resp = run_worker(subcommand, payload, timeout_s=timeout_s, redactor=redactor)
    if resp.ok:
        return resp
    if require_real:
        return resp
    # Fallback simulado: nunca fabricar pesos, solo devolver estructura vacía
    # con `simulated: true` para que el llamador decida.
    return MlWorkerResponse(
        ok=True,
        subcommand=subcommand,
        payload={
            "simulated": True,
            "simulation_notice": "worker no disponible; respuesta simulada",
            "worker_error": resp.error,
        },
        exit_code=resp.exit_code,
        stderr_tail=resp.stderr_tail,
    )


def probe_ml(
    interpreter: str | None = None,
    timeout_s: float = 30.0,
    redactor: SecretRedactor | None = None,
) -> dict[str, Any]:
    """Ejecuta `worker.py probe` y devuelve las capacidades del entorno."""
    return probe_capabilities(interpreter=interpreter, redactor=redactor).to_public_dict()
