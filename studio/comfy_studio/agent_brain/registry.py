"""Registro de capacidades del cerebro del agente (M6-SCR, Bloque 1).

Un lugar único donde el lazo de orquestación y el strict-check descubren las
capacidades del cerebro de forma uniforme, igual que `engines/registry.py` hace
con los motores. No arranca servicios ni toca GPU: solo cataloga y consulta
`available()`/`is_stub()` de cada capacidad.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.agent_brain.base import BrainCapability
from comfy_studio.agent_brain.capabilities import (
    CompressCapability,
    MemoryCapability,
    RerankCapability,
    RetrieveCapability,
)


class BrainRegistry:
    """Catálogo de capacidades del cerebro. No arranca nada; describe y enruta."""

    def __init__(self) -> None:
        self._caps: dict[str, BrainCapability] = {}

    def register(self, cap: BrainCapability) -> None:
        if cap.name in self._caps:
            raise ValueError(f"capacidad duplicada en el cerebro: {cap.name!r}")
        self._caps[cap.name] = cap

    def get(self, name: str) -> BrainCapability | None:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return sorted(self._caps)

    def describe(self) -> dict[str, Any]:
        """Resumen serializable para el API del panel y el strict-check."""
        return {
            "count": len(self._caps),
            "capabilities": [
                {
                    "name": self._caps[n].name,
                    "tier": str(self._caps[n].tier),
                    "available": self._caps[n].available(),
                    "stub": self._caps[n].is_stub(),
                }
                for n in self.names()
            ],
        }


def build_default_brain() -> BrainRegistry:
    """Cerebro por defecto: las cuatro capacidades del grupo B del plan maestro."""
    reg = BrainRegistry()
    reg.register(MemoryCapability())
    reg.register(CompressCapability())
    reg.register(RetrieveCapability())
    reg.register(RerankCapability())
    return reg


def brain_health_report(registry: BrainRegistry) -> dict[str, Any]:
    """Reporte serializable del estado del cerebro (para /studio/api/health, S16)."""
    desc = registry.describe()
    caps = desc["capabilities"]
    counts = {"ready": 0, "stub": 0, "unavailable": 0}
    for c in caps:
        if c["stub"]:
            counts["stub"] += 1
        elif c["available"]:
            counts["ready"] += 1
        else:
            counts["unavailable"] += 1
    return {"counts": counts, "capabilities": caps}
