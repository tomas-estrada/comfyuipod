"""Protocolo de workers de ML en subprocesso (ComfyUI interpreter, M3.1).

Los workers se ejecutan como procesos hijo aislados vía `/usr/bin/python3`
(overridable con `STUDIO_ML_PYTHON_BIN`). Reciben una petición JSON en stdin
y devuelven una respuesta JSON en stdout. El ruido de diagnóstico va a stderr.

Subcommandos del worker:
  - probe: detección de capacidades (intérprete, torch, open_clip, faster_whisper,
           funasr, insightface, pesos, CUDA).
  - clip_embed_text / clip_embed_image: embeddings CLIP (768-d, ViT-L/14).
  - faces_detect: detección e embeddings faciales InsightFace buffalo_l (512-d).
  - asr_transcribe: transcripción faster-whisper large-v3 + alineamiento.
  - diarize: diarización cam++ (FunASR) + fusión con transcripción.

Ningún worker lee STUDIO_LLM_KEY, COMFY_PASS, STUDIO_API_TOKEN ni
STUDIO_WEBHOOK_SECRET: el entorno del hijo se construye con allowlist.
Toda carga y todo stderr pasan por SecretRedactor antes de llegar a un log o
respuesta HTTP.
"""

from __future__ import annotations

import os


class MlWorkerResponse:
    """Respuesta tipada de un worker de ML."""

    def __init__(
        self,
        *,
        ok: bool,
        subcommand: str,
        payload: dict | None = None,
        error: str | None = None,
        exit_code: int | None = None,
        stderr_tail: str | None = None,
    ) -> None:
        self.ok = ok
        self.subcommand = subcommand
        self.payload = payload or {}
        self.error = error
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


# Constantes del protocolo (definidas antes de los submódulos para evitar
# import circular: probe.py y runner.py las importan de `comfy_studio.ml`).
WORKER_ENTRY = "worker.py"
MlWorkerSubcommand = str

ML_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "probe",
        "clip_embed_text",
        "clip_embed_image",
        "faces_detect",
        "asr_transcribe",
        "diarize",
    }
)

# Timeouts por defecto por subcomando (segundos).
ML_DEFAULT_TIMEOUTS: dict[str, float] = {
    "probe": 30.0,
    "clip_embed_text": 120.0,
    "clip_embed_image": 120.0,
    "faces_detect": 120.0,
    "asr_transcribe": 300.0,
    "diarize": 120.0,
}

# Variables de entorno permitidas en el entorno del worker hijo (allowlist:
# nunca se hereda os.environ.copy() completo para evitar fugas de secretos).
ML_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LANG",
        "LC_ALL",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "COMFY_DATA_DIR",
        "STUDIO_DATA_DIR",
        "CUDA_VISIBLE_DEVICES",
        "TORCH_CUDA_ARCH_LIST",
    }
)


from .probe import MlCapabilities, probe_capabilities  # noqa: E402
from .runner import (  # noqa: E402
    MlWorkerError,
    MlWorkerTimeout,
    run_worker,
)

__all__ = [
    "MlCapabilities",
    "MlWorkerError",
    "MlWorkerTimeout",
    "MlWorkerResponse",
    "MlWorkerSubcommand",
    "ML_SUBCOMMANDS",
    "ML_DEFAULT_TIMEOUTS",
    "ML_ENV_ALLOWLIST",
    "WORKER_ENTRY",
    "is_real_backend",
    "resolve_perception_backend",
    "probe_capabilities",
    "run_worker",
]


def resolve_perception_backend() -> str:
    """Resuelve el backend de percepción desde `STUDIO_PERCEPTION_BACKEND`.

    Valores: `auto` (default), `real`, `sim`.
    """
    return (os.environ.get("STUDIO_PERCEPTION_BACKEND") or "auto").strip().lower()


def is_real_backend() -> bool:
    """True si el backend configurado es `real` (sin fallback a simulación)."""
    return resolve_perception_backend() == "real"


def require_real_payload(resp: MlWorkerResponse, subcommand: str) -> dict:
    """Valida una respuesta de worker en modo `real`.

    Si el worker falló o devolvió `simulated: True` (no pudo cargar modelos
    reales), lanza `RuntimeError` — nunca se falla silenciosamente a sim.
    """
    if not resp.ok:
        raise RuntimeError(
            f"worker {subcommand} falló: {resp.error or 'error desconocido'}"
        )
    payload = resp.payload or {}
    if payload.get("simulated") is True:
        raise RuntimeError(
            f"worker {subcommand} en modo real devolvió simulación; "
            f"los modelos reales no están disponibles. Error: {resp.error or 'n/a'}"
        )
    return payload
