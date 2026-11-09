"""Cliente del worker de ML en subprocesso (M3.1).

`run_worker(subcommand, payload, timeout_s)` lanza `worker.py` como proceso
hijo con `subprocess.run(capture_output=True, timeout=...)`, nunca `shell=True`.
Mapea códigos de salida a errores tipados; toda falla lleva el tail de stderr
redactado. El timeout se pasa por el parámetro `timeout_s` (si es `> 0`) o
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


def _build_env() -> dict[str, str]:
    """Entorno del hijo: allowlist estricta, sin secretos.

    Siempre devuelve un dict explícito (posiblemente vacío). Nunca `None`:
    con `env=None` `subprocess.run` heredaría el entorno completo del padre,
    incluyendo posibles secretos fuera de la allowlist.
    """
    env: dict[str, str] = {}
    for key in sorted(ML_ENV_ALLOWLIST):
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


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
    # `from_env()` y no un `SecretRedactor()` pelado: el pelado no tiene ningún
    # secreto literal, así que un `stderr_tail` que los contenga saldría sin
    # máscara. `from_env()` los toma del entorno real del proceso.
    redactor = redactor or SecretRedactor.from_env()
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
    except (FileNotFoundError, PermissionError) as exc:
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"intérprete no encontrado o no ejecutable: {interpreter}: {exc}",
            exit_code=None,
            stderr_tail=redactor.redact(str(exc)),
        )
    except Exception as exc:
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=redactor.redact(f"{type(exc).__name__}: {exc}"),
            exit_code=None,
            stderr_tail=redactor.redact(str(exc)),
        )

    # stdout vacío con rc==0 es un fallo tipado, nunca un éxito vacío.
    if res.returncode == 0 and not (res.stdout or "").strip():
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"salida del worker vacía (rc=0, subcomando {subcommand})",
            exit_code=res.returncode,
            stderr_tail=redactor.redact(res.stderr or ""),
        )

    # Parsear stdout JSON
    try:
        parsed = json.loads(res.stdout) if (res.stdout or "").strip() else {}
    except Exception as exc:
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=f"salida del worker no es JSON válido: {exc}",
            exit_code=res.returncode,
            stderr_tail=redactor.redact(res.stderr or ""),
        )

    if res.returncode != 0:
        worker_error = parsed.get("error") if isinstance(parsed, dict) else None
        worker_tail = parsed.get("stderr_tail") if isinstance(parsed, dict) else None
        return MlWorkerResponse(
            ok=False,
            subcommand=subcommand,
            error=redactor.redact(
                str(worker_error)
                if worker_error
                else f"worker {subcommand} salió con rc={res.returncode}"
            ),
            exit_code=res.returncode,
            stderr_tail=redactor.redact(
                worker_tail if worker_tail else (res.stderr or "")
            ),
            payload=parsed.get("payload") if isinstance(parsed, dict) else None,
        )

    ok_tail = parsed.get("stderr_tail") if isinstance(parsed, dict) else None
    return MlWorkerResponse(
        ok=True,
        subcommand=subcommand,
        payload=parsed.get("payload", {}) if isinstance(parsed, dict) else {},
        exit_code=res.returncode,
        stderr_tail=redactor.redact(str(ok_tail)) if ok_tail else None,
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
