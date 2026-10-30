"""Capacidades del cerebro, stub-first (M6-SCR, Bloque 1, fase 1).

Fase 1: solo los stubs deterministas + el contrato. Los backends reales entran
en fases posteriores:
  - compress  → LLMLingua-2 (fase 2)
  - retrieve  → BM25s     (fase 3)
  - rerank    → FlashRank (fase 3)
  - memory    → Letta     (fase 4)

En modo stub cada capacidad devuelve un resultado DETERMINISTA y honesto
(`simulated=True`), nunca finge trabajo real: CI verde sin pesos/LLM/servicio.
"""

from __future__ import annotations

from typing import Any

from comfy_studio.agent_brain import BrainCapabilityTier, BrainResult
from comfy_studio.agent_brain.base import BrainCapability


class MemoryCapability(BrainCapability):
    """Memoria persistente por proyecto (Letta en fase 4). Ops: remember / recall."""

    name = "memory"
    tier = BrainCapabilityTier.WARM

    def available(self) -> bool:
        return self.is_stub()  # fase 4 añade el backend Letta real

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op not in ("remember", "recall"):
            return self._unknown_op(op)
        if self.is_stub():
            if op == "remember":
                return BrainResult(
                    ok=True,
                    capability=self.name,
                    op=op,
                    payload={"stored": True, "key": str(args.get("key", ""))},
                    simulated=True,
                )
            # recall: stub devuelve lista vacía honesta (no inventa memorias)
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"memories": []},
                simulated=True,
            )
        return BrainResult(
            ok=False,
            capability=self.name,
            op=op,
            error="backend de memoria (Letta) no disponible; usa STUDIO_STUB_BRAIN_MEMORY=1",
        )


class CompressCapability(BrainCapability):
    """Compresión de contexto (LLMLingua-2 en fase 2). Op: compress."""

    name = "compress"
    tier = BrainCapabilityTier.CPU

    def available(self) -> bool:
        return self.is_stub()  # fase 2 añade LLMLingua-2 real

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op != "compress":
            return self._unknown_op(op)
        text = str(args.get("text", ""))
        if self.is_stub():
            # Stub honesto: no comprime (devuelve el texto tal cual), marca ratio=1.0.
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"compressed": text, "ratio": 1.0, "tokens_in": len(text.split())},
                simulated=True,
            )
        return BrainResult(
            ok=False,
            capability=self.name,
            op=op,
            error=(
                "backend de compresión (LLMLingua-2) no disponible; "
                "usa STUDIO_STUB_BRAIN_COMPRESS=1"
            ),
        )


class RetrieveCapability(BrainCapability):
    """Recuperación léxica BM25 (BM25s en fase 3). Op: search."""

    name = "retrieve"
    tier = BrainCapabilityTier.CPU

    def available(self) -> bool:
        return self.is_stub()  # fase 3 añade BM25s real

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op != "search":
            return self._unknown_op(op)
        if self.is_stub():
            # Stub determinista: devuelve los primeros k docs tal cual, sin ranking real.
            docs = list(args.get("docs", []))
            k = int(args.get("k", 3))
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"hits": docs[:k], "scored": False},
                simulated=True,
            )
        return BrainResult(
            ok=False,
            capability=self.name,
            op=op,
            error="backend de recuperación (BM25s) no disponible; usa STUDIO_STUB_BRAIN_RETRIEVE=1",
        )


class RerankCapability(BrainCapability):
    """Re-ranking (FlashRank en fase 3). Op: rerank."""

    name = "rerank"
    tier = BrainCapabilityTier.CPU

    def available(self) -> bool:
        return self.is_stub()  # fase 3 añade FlashRank real

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op != "rerank":
            return self._unknown_op(op)
        if self.is_stub():
            # Stub determinista: conserva el orden de entrada (identidad), top-k.
            candidates = list(args.get("candidates", []))
            k = int(args.get("k", 3))
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"ranked": candidates[:k], "reordered": False},
                simulated=True,
            )
        return BrainResult(
            ok=False,
            capability=self.name,
            op=op,
            error="backend de re-ranking (FlashRank) no disponible; usa STUDIO_STUB_BRAIN_RERANK=1",
        )
