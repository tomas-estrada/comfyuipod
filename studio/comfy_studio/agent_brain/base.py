"""Clase base de una capacidad del cerebro del agente (M6-SCR, Bloque 1).

Cada capacidad (memory, compress, retrieve, rerank) implementa un contrato
uniforme. El lazo de orquestación las ve como herramientas iguales, igual que ve
los motores. Ningún método lanza por fallo: devuelven `BrainResult(ok=False, ...)`.
"""

from __future__ import annotations

import abc
from typing import Any

from comfy_studio.agent_brain import (
    BrainCapabilityTier,
    BrainResult,
    is_brain_stub,
)


class BrainCapability(abc.ABC):
    """Interfaz uniforme de una capacidad del cerebro."""

    #: identificador corto, p.ej. "memory", "compress", "retrieve", "rerank".
    name: str = "capability"
    #: nivel de residencia (CPU barata vs WARM que carga al primer uso).
    tier: BrainCapabilityTier = BrainCapabilityTier.CPU

    def is_stub(self) -> bool:
        """True si esta capacidad corre en modo stub (CI sin pesos/servicio)."""
        return is_brain_stub(self.name)

    @abc.abstractmethod
    def available(self) -> bool:
        """True si el backend real está disponible (o estamos en stub)."""

    @abc.abstractmethod
    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        """Ejecutar una operación de la capacidad.

        `op` es específico de la capacidad (p.ej. memory: remember/recall;
        compress: compress; retrieve: search; rerank: rerank). Toda falla se
        tipifica en `BrainResult`.
        """

    # -- helper común --------------------------------------------------------

    def _unknown_op(self, op: str) -> BrainResult:
        return BrainResult(
            ok=False,
            capability=self.name,
            op=op,
            error=f"op desconocida para la capacidad '{self.name}': {op!r}",
        )
