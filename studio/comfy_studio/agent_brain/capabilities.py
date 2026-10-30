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
    """Compresión de contexto (LLMLingua-2). Op: compress.

    Real: poda 2-5x de historiales/salidas de herramienta vía el modelo
    `llmlingua-2-bert-base-multilingual` (CPU o ~0.5-1 GB). El modelo se carga
    perezosamente al primer uso y se cachea en la instancia. Sin la lib
    `llmlingua` instalada, falla TIPADO (nunca lanza). En stub, identidad honesta.
    """

    name = "compress"
    tier = BrainCapabilityTier.CPU

    _MODEL = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"

    def __init__(self) -> None:
        self._compressor: object | None = None  # carga perezosa

    def available(self) -> bool:
        if self.is_stub():
            return True
        try:
            import importlib.util

            return importlib.util.find_spec("llmlingua") is not None
        except ImportError:  # pragma: no cover - find_spec no lanza normalmente
            return False

    def _load(self) -> object | None:
        if self._compressor is not None:
            return self._compressor
        try:
            from llmlingua import PromptCompressor
        except ImportError:
            return None
        self._compressor = PromptCompressor(
            model_name=self._MODEL, use_llmlingua2=True
        )
        return self._compressor

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
        compressor = self._load()
        if compressor is None:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=(
                    "backend de compresión (LLMLingua-2) no disponible; "
                    "instala `llmlingua` en la imagen o usa STUDIO_STUB_BRAIN_COMPRESS=1"
                ),
            )
        rate = float(args.get("rate", 0.4))
        force_tokens = list(
            args.get("force_tokens", ["\n", "User:", "Assistant:", "tool_call:", "status:"])
        )
        try:
            res = compressor.compress_prompt([text], rate=rate, force_tokens=force_tokens)  # type: ignore[attr-defined]
        except (RuntimeError, ValueError, OSError) as exc:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=f"LLMLingua-2 falló: {type(exc).__name__}: {exc}",
            )
        compressed = str(res.get("compressed_prompt", text))
        tokens_in = len(text.split())
        tokens_out = len(compressed.split())
        ratio = round(tokens_out / tokens_in, 4) if tokens_in else 1.0
        return BrainResult(
            ok=True,
            capability=self.name,
            op=op,
            payload={"compressed": compressed, "ratio": ratio, "tokens_in": tokens_in},
        )


class RetrieveCapability(BrainCapability):
    """Recuperación léxica BM25 (BM25s). Op: search.

    Real: índice BM25 en NumPy sobre los `docs` dados, devuelve los top-k por
    score léxico. Complementa la búsqueda vectorial de `perception/store.py`.
    Sin `bm25s`, falla TIPADO. En stub, top-k en orden de entrada (sin score).
    """

    name = "retrieve"
    tier = BrainCapabilityTier.CPU

    def available(self) -> bool:
        if self.is_stub():
            return True
        import importlib.util

        return importlib.util.find_spec("bm25s") is not None

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op != "search":
            return self._unknown_op(op)
        docs = [str(d) for d in args.get("docs", [])]
        query = str(args.get("query", ""))
        k = int(args.get("k", 3))
        if self.is_stub():
            # Stub determinista: devuelve los primeros k docs tal cual, sin ranking real.
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"hits": docs[:k], "scored": False},
                simulated=True,
            )
        try:
            import bm25s
        except ImportError:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=(
                    "backend de recuperación (BM25s) no disponible; "
                    "instala `bm25s` en la imagen o usa STUDIO_STUB_BRAIN_RETRIEVE=1"
                ),
            )
        if not docs:
            return BrainResult(
                ok=True, capability=self.name, op=op, payload={"hits": [], "scored": True}
            )
        try:
            corpus_tokens = bm25s.tokenize(docs, stopwords=None)
            retriever = bm25s.BM25()
            retriever.index(corpus_tokens)
            query_tokens = bm25s.tokenize(query, stopwords=None)
            results, _scores = retriever.retrieve(
                query_tokens, k=min(k, len(docs)), corpus=docs
            )
            hits = [str(h) for h in results[0]]
        except (RuntimeError, ValueError) as exc:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=f"BM25s falló: {type(exc).__name__}: {exc}",
            )
        return BrainResult(
            ok=True,
            capability=self.name,
            op=op,
            payload={"hits": hits, "scored": True},
        )


class RerankCapability(BrainCapability):
    """Re-ranking (FlashRank). Op: rerank.

    Real: reordena `candidates` por relevancia a `query` con un cross-encoder
    ONNX en CPU (sin PyTorch). Caso de uso: top-50 de la recuperación → top-3.
    Sin `flashrank`, falla TIPADO. En stub, identidad (conserva el orden), top-k.
    """

    name = "rerank"
    tier = BrainCapabilityTier.CPU

    def __init__(self) -> None:
        self._ranker: object | None = None  # carga perezosa

    def available(self) -> bool:
        if self.is_stub():
            return True
        import importlib.util

        return importlib.util.find_spec("flashrank") is not None

    def _load(self) -> object | None:
        if self._ranker is not None:
            return self._ranker
        try:
            from flashrank import Ranker
        except ImportError:
            return None
        self._ranker = Ranker()
        return self._ranker

    def run(self, op: str, args: dict[str, Any]) -> BrainResult:
        if op != "rerank":
            return self._unknown_op(op)
        candidates = [str(c) for c in args.get("candidates", [])]
        query = str(args.get("query", ""))
        k = int(args.get("k", 3))
        if self.is_stub():
            # Stub determinista: conserva el orden de entrada (identidad), top-k.
            return BrainResult(
                ok=True,
                capability=self.name,
                op=op,
                payload={"ranked": candidates[:k], "reordered": False},
                simulated=True,
            )
        ranker = self._load()
        if ranker is None:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=(
                    "backend de re-ranking (FlashRank) no disponible; "
                    "instala `flashrank` en la imagen o usa STUDIO_STUB_BRAIN_RERANK=1"
                ),
            )
        if not candidates:
            return BrainResult(
                ok=True, capability=self.name, op=op, payload={"ranked": [], "reordered": True}
            )
        try:
            from flashrank import RerankRequest

            passages = [{"id": i, "text": c} for i, c in enumerate(candidates)]
            ranked = ranker.rerank(RerankRequest(query=query, passages=passages))  # type: ignore[attr-defined]
            ordered = [str(r["text"]) for r in ranked[:k]]
        except (RuntimeError, ValueError, KeyError) as exc:
            return BrainResult(
                ok=False,
                capability=self.name,
                op=op,
                error=f"FlashRank falló: {type(exc).__name__}: {exc}",
            )
        return BrainResult(
            ok=True,
            capability=self.name,
            op=op,
            payload={"ranked": ordered, "reordered": True},
        )
