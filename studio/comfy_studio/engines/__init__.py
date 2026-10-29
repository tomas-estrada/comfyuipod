"""Contrato uniforme de motores del estudio (M4-SCR-001).

Generaliza el patrón ya probado en `comfy_studio/blender.py` (subproceso
aislado, modos de trabajo, `STUDIO_STUB_*`, timeout, redacción) y en
`comfy_studio/ml/runner.py` (respuesta **tipada**, nunca `shell=True`,
`require_real` vs fallback simulado) hacia una interfaz única que el agente ve
como herramientas iguales.

Las tres capas (ver `docs/propuestas/ESTUDIO_REEL_MOTORES.md`):
  - **Motor**: servicio caliente, horneado en imagen. Expone 4 verbos.
  - **Submotor**: nodo ComfyUI / add-on Blender; el agente lo trae on the run
    (el motor host ya está caliente, sin GPU ociosa).
  - **Modelo**: checkpoint/LoRA/voz; siempre on the run.

Los **4 verbos** de todo motor:
  - `list`    → qué submotores/capacidades hay (ComfyUI: GET /object_info).
  - `install` → traer el submotor que falta (bajo PIP_CONSTRAINT / nodes.lock).
  - `verify`  → confirmar que cargó antes de usar.
  - `run`     → ejecutar el trabajo.

Reglas de `AGENTS.md` que este contrato respeta por diseño:
  - toda respuesta es tipada (`EngineResult`); ninguna falla lanza hacia el
    servidor sin tipificar,
  - todo stderr/log pasa por `SecretRedactor`,
  - todo `run` corre con timeout y es cancelable,
  - cada motor mantiene un modo stub (`STUDIO_STUB_<MOTOR>=1`) para tests sin
    binario y sin GPU.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Verbos del contrato (frozenset, igual que ML_SUBCOMMANDS)
# ---------------------------------------------------------------------------

ENGINE_VERBS: frozenset[str] = frozenset({"list", "install", "verify", "run"})

# Timeouts por defecto por verbo (segundos). `run` depende del motor y se
# sobrescribe por `EngineRunSpec.timeout_s`; estos son suelos razonables.
ENGINE_DEFAULT_TIMEOUTS: dict[str, float] = {
    "list": 30.0,
    "install": 900.0,  # instalar submotor puede tardar; nunca es el camino rápido
    "verify": 30.0,
    "run": 3600.0,
}


class EngineTier(StrEnum):
    """Nivel de residencia en GPU (M4-OPS-001)."""

    RESIDENT = "resident"  # siempre en GPU (ComfyUI)
    WARM = "warm"  # instalado + sano, carga al primer uso (segundos)
    INSTALLABLE = "installable"  # no horneado; el agente puede instalarlo (minutos, puerta §6)


# ---------------------------------------------------------------------------
# Respuesta tipada (espejo de MlWorkerResponse)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EngineResult:
    """Resultado tipado de cualquier verbo de motor.

    Nunca se lanza excepción por fallo del motor: todo se tipifica aquí, igual
    que `MlWorkerResponse`. `stderr_tail` llega ya redactado.
    """

    ok: bool
    engine: str
    verb: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    exit_code: int | None = None
    stderr_tail: str | None = None
    simulated: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """Forma segura para exponer por el API (sin objetos internos)."""
        return {
            "ok": self.ok,
            "engine": self.engine,
            "verb": self.verb,
            "payload": self.payload,
            "error": self.error,
            "exit_code": self.exit_code,
            "stderr_tail": self.stderr_tail,
            "simulated": self.simulated,
        }


@dataclass(slots=True)
class EngineRunSpec:
    """Parámetros de un `run`: qué trabajo y con qué límites."""

    job: dict[str, Any]
    timeout_s: float | None = None
    cost_cap: float | None = None  # tope de coste/gasto GPU para este run


# ---------------------------------------------------------------------------
# Helpers de modo stub y nombres de env (espejo de is_stub_mode en blender.py)
# ---------------------------------------------------------------------------


def stub_env_name(engine: str) -> str:
    """Nombre de la env var de stub de un motor, p.ej. `STUDIO_STUB_BLENDER`."""
    return f"STUDIO_STUB_{engine.strip().upper()}"


def is_stub_mode(engine: str) -> bool:
    """True si el motor está en modo stub (tests sin binario/GPU)."""
    val = (os.environ.get(stub_env_name(engine)) or "").strip().lower()
    return val in ("1", "true", "yes")


def is_dynamic_enabled() -> bool:
    """Interruptor maestro `STUDIO_DYNAMIC=1` (M4-SCR, §8 del doc).

    Off (default) = pod determinista; el agente NO instala submotores ni motores.
    On = el agente puede componer grafos, instalar submotores y (con la puerta
    de decisión §6) instalar motores.
    """
    val = (os.environ.get("STUDIO_DYNAMIC") or "").strip().lower()
    return val in ("1", "true", "yes")
