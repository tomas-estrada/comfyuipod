# Research retrieval for builder agents

The investigation document in this directory is the initial evidence corpus.
Its bibliographic claims came from Consensus search and have **not** been
independently verified; it mixes peer-reviewed work, preprints, and reports.
Use it to find candidate approaches, then verify important claims against the
linked primary paper before relying on them.

## Query the corpus

From the repository root:

```bash
python scripts/research_rag.py "How should artifact retries and lineage work?"
```

The offline CLI uses BM25 over Markdown section/paragraph chunks. It needs no
network, model download, embedding service, or additional Python package. The
result includes the source file and section for every passage. Add future
research notes under this directory; the CLI discovers Markdown recursively.

## Builder-agent rule

Before choosing an architecture for agent execution, MCP/tool security,
3D/Blender, multimodal validation, or context management, query this corpus.
Use retrieved passages to form a hypothesis, inspect the corresponding papers
and repository code, and test the proposed change. Do not turn correlation or
benchmark results from other domains into a guarantee about ComfyUIPod. Record
both the evidence boundary and CPU-versus-hardware validation needed in the PR.

This is lexical retrieval augmented generation support, not a generative model
or a claim of semantic completeness. If retrieval misses, refine the query and
inspect the full report.
