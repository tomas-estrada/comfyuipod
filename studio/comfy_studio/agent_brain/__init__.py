"""Cerebro del agente del estudio (M6-SCR, Bloque 1).

El estudio no es solo motores de render: para convertir una idea abstracta en una
producción el agente necesita un **cerebro capaz** — memoria persistente por
proyecto, compresión de contexto, y recuperación/re-ranking. Este subsistema es
ese cerebro. NO es un `Engine` de render (no produce frames): es la capa que el
lazo de orquestación (M6 Bloque 4) consulta para razonar con contexto largo sin
arrastrar historiales enteros.

Sigue el mismo contrato probado de `comfy_studio/engines/`:
  - toda respuesta es **tipada** (`BrainResult`); ninguna falla lanza sin tipificar,
  - cada capacidad mantiene un **modo stub** (`STUDIO_STUB_BRAIN_<NAME>=1`) para
    CI sin pesos/LLM,
  - subsistemas reales aislados, con redacción de secretos,
  - `require_real` vs fallback simulado, igual que `ml/runner.py`.

Capacidades (grupo B del plan maestro `PLAN_MAESTRO_ESTUDIO_AGENTE.md`):
  - `memory`    → memoria persistente por proyecto (Letta): recordar personajes,
                  paletas, acuerdos de estilo; recuperar por similitud.
  - `compress`  → compresión de contexto (LLMLingua-2): podar historiales/salidas
                  de herramienta 2–5x sin API externa.
  - `retrieve`  → recuperación léxica (BM25s) sobre guiones/diálogos/metadatos.
  - `rerank`    → re-ranking (FlashRank): top-50 → top-3, CPU, determinista.

Todo el subsistema corre loopback-only y respeta la regla n.º 6 de AGENTS.md
(sin pip en runtime fuera de la imagen/install_gate).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Capacidades del cerebro (análogo a ENGINE_VERBS, pero por capacidad)
# ---------------------------------------------------------------------------

BRAIN_CAPABILITIES: frozenset[str] = frozenset(
    {"memory", "compress", "retrieve", "rerank"}
)


class BrainCapabilityTier(StrEnum):
    """Nivel de residencia de una capacidad del cerebro."""

    #: siempre disponible, barata, CPU (compress / retrieve / rerank).
    CPU = "cpu"
    #: requiere un servicio o modelo que carga al primer uso (memory/Letta).
    WARM = "warm"


# ---------------------------------------------------------------------------
# Respuesta tipada (espejo de EngineResult / MlWorkerResponse)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BrainResult:
    """Resultado tipado de cualquier operación del cerebro.

    Nunca se lanza excepción por fallo: todo se tipifica aquí, igual que
    `EngineResult`. `stderr_tail` (si lo hay) llega ya redactado.
    """

    ok: bool
    capability: str
    op: str
    payload: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    simulated: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        """Forma segura para exponer por el API (sin objetos internos)."""
        return {
            "ok": self.ok,
            "capability": self.capability,
            "op": self.op,
            "payload": self.payload,
            "error": self.error,
            "simulated": self.simulated,
        }


# ---------------------------------------------------------------------------
# Helpers de modo stub y nombres de env (espejo de is_stub_mode en engines/)
# ---------------------------------------------------------------------------


def stub_env_name(capability: str) -> str:
    """Env var de stub de una capacidad, p.ej. `STUDIO_STUB_BRAIN_MEMORY`."""
    return f"STUDIO_STUB_BRAIN_{capability.strip().upper()}"


def is_brain_stub(capability: str) -> bool:
    """True si la capacidad corre en modo stub (CI sin pesos/LLM/servicio)."""
    val = (os.environ.get(stub_env_name(capability)) or "").strip().lower()
    return val in ("1", "true", "yes")
