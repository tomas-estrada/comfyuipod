#!/usr/bin/env python3
"""Small offline retrieval tool for the repository's evidence-backed research.

Uses section/paragraph chunks and BM25 ranking so it works without a vector
database, embedding model, network access, or runtime dependencies.
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "docs" / "research"
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_'-]*", re.IGNORECASE)


@dataclass(frozen=True)
class Chunk:
    source: Path
    heading: str
    text: str

    @property
    def tokens(self) -> list[str]:
        return [token.lower() for token in TOKEN_RE.findall(f"{self.heading} {self.text}")]


def read_chunks(corpus: Path) -> list[Chunk]:
    """Read Markdown research notes, preserving heading and source provenance."""
    chunks: list[Chunk] = []
    for path in sorted(corpus.rglob("*.md")):
        if path.name.lower() in {"readme.md", "index.md"}:
            continue
        heading = path.stem
        paragraphs: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("#"):
                if paragraphs:
                    chunks.append(Chunk(path, heading, "\n".join(paragraphs).strip()))
                    paragraphs = []
                heading = line.lstrip("# ").strip() or path.stem
            elif line.strip():
                paragraphs.append(line.strip())
            elif paragraphs:
                chunks.append(Chunk(path, heading, "\n".join(paragraphs).strip()))
                paragraphs = []
        if paragraphs:
            chunks.append(Chunk(path, heading, "\n".join(paragraphs).strip()))
    return chunks


def search(query: str, chunks: list[Chunk], top_k: int = 5) -> list[tuple[float, Chunk]]:
    """Return BM25-scored chunks; repeated query terms contribute once."""
    terms = set(token.lower() for token in TOKEN_RE.findall(query))
    if not terms or not chunks:
        return []
    tokenized = [chunk.tokens for chunk in chunks]
    doc_freq = Counter(term for doc in tokenized for term in set(doc))
    avg_len = sum(map(len, tokenized)) / len(tokenized)
    k1, b = 1.5, 0.75
    ranked: list[tuple[float, Chunk]] = []
    for chunk, doc in zip(chunks, tokenized, strict=True):
        counts = Counter(doc)
        score = 0.0
        for term in terms:
            tf = counts[term]
            if tf == 0:
                continue
            idf = math.log(1 + (len(chunks) - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            norm = tf + k1 * (1 - b + b * len(doc) / max(avg_len, 1))
            score += idf * tf * (k1 + 1) / norm
        if score:
            ranked.append((score, chunk))
    return sorted(ranked, key=lambda item: item[0], reverse=True)[: max(top_k, 0)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="question or implementation topic")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    chunks = read_chunks(args.corpus)
    results = search(args.query, chunks, args.top_k)
    if not results:
        print("No matching research passages. State that the corpus has no evidence.")
        return 1
    print("Retrieved evidence passages. Treat findings as guidance, not proof for this repository.")
    print("For decisions, check the cited papers' study design, limits, and applicability.")
    for rank, (score, chunk) in enumerate(results, 1):
        source = (
            chunk.source.relative_to(ROOT)
            if chunk.source.is_relative_to(ROOT)
            else chunk.source
        )
        print(f"\n[{rank}] score={score:.3f} source={source} section={chunk.heading}")
        print(chunk.text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
