# ComfyUI and Blender ecosystem catalog review

**Snapshot:** 2026-11-12. This note analyzes the two user-provided enriched CSVs
and checks selected primary pages with Exa. The complete row-level data remains
available in the [ComfyUI CSV](../datasets/comfyui-ecosystem-2026-11-12.csv) and
[Blender/3D CSV](../datasets/blender-3d-ecosystem-2026-11-12.csv); the offline
builder RAG indexes each row with its URL and row provenance.

## Coverage and limits

| Catalog | Rows | Unique nonempty URLs | Blank URL cells | Fields | Distinct hosts |
|---|---:|---:|---:|---:|---:|
| ComfyUI | 466 | 464 | 2 | 45 | 49 |
| Blender and open 3D | 314 | 312 | 2 | 15 | 63 |

The ComfyUI rows contain 11,906 citation references across 2,021 distinct
citation URLs; the Blender rows contain 2,620 references across 681 distinct
citation URLs. These are counts of the provided dataset, not independent
verification of every citation. The ComfyUI taxonomy is broad: models, custom
nodes/extensions, documentation, tutorials, workflows and tools dominate. The
Blender dataset also includes learning materials, films, alternatives and
commercial products alongside actual add-ons. A record's presence is not an
adoption recommendation.

The ComfyUI CSV has 75 `canonical_url` cells set to `skipped` and 81 raw/canonical
URL differences. Differences include tracking-query removal, deliberate skips
and renamed repositories, so they need per-row interpretation rather than
being counted as broken links. It has four negative and three pending
`distinct_resource_check` results. Blender's own filters identify three
non-ecosystem records and one pending record; its `current_or_significant_check`
also marks five rows negative. Neither file has duplicate nonempty raw URLs in
the measured input; both have two blank URLs.

The quality audit found that its first automated sample-labeling pass had an
off-by-one row/title mismatch. Those sample labels are excluded as evidence of
source errors. CSV basis fields parse as JSON in all rows. The dataset itself
contains enriched descriptions, checks and source excerpts; claims still need
verification against the cited source before implementation. “Yes” in the
ComfyUI `malware_nsfw_check` field means the catalog check passed; it is not a
malware warning. Inference: the catalog is most useful as a discovery/indexing
resource, with explicit review gates before execution.

## Project fit

ComfyUIPod already has a ComfyUI API adapter, curated templates, async jobs,
workflow checks and a role-scoped tool belt. It also has the Blender subprocess
engine, the `artist3d` role and a pinned offline Blender Extensions metadata
catalog with 1,162 entries. The current catalog search is discovery-only: it
cannot install or execute extensions. The existing model-connector research
recommends extending these adapters and keeping external providers behind
explicit task boundaries rather than adding a second unrestricted agent or
daemon. See [engine/model connector guidance](../engine-model-connectors.md),
[Blender extension policy](../blender-extensions-integration.md), and the
[capability evidence ledger](../../ultra/CAPABILITY_EVIDENCE.md).

## Ranked follow-up options

The weighted scores below are a deterministic triage judgment, not measured
performance: each dimension is 0–5 (`capability gap fit` 30%, `novelty` 20%,
`integration speed` 20%, `license/security fit` 15%, `resource fit` 15%). The
dimension tuple is shown as `fit/novelty/speed/policy/resources`. Scores
prioritize investigation or reuse, never automatic installation.

| Rank | Finding | Score (dimensions) | Recommended next step | Evidence boundary |
|---:|---|---:|---|---|
| 1 | Indexing the source databases in the local builder RAG makes all 776 nonempty URLs and their enrichment searchable without adding runtime engines. | 4.8 (5/4/5/5/5) | Preserve both CSVs unchanged, retain per-row provenance, mark rows external/unverified, and retrieve them by task; the CSV RAG implementation and focused tests are part of this change. | The original row descriptions and citations are user-provided enrichment, not independently confirmed claims. Selected pages below were checked with Exa. |
| 2 | Official ComfyUI agent-tool docs describe Cloud and local MCP connections, Comfy Agent and Comfy CLI. | 4.3 (4/3/5/5/5) | Use the published tool surface as a parity checklist for read-only node/model discovery, schema inspection, workflow validation, job status and output retrieval in the existing Studio adapter. Do not add a duplicate MCP server. | The [official guide](https://docs.comfy.org/agent-tools) was fetched with Exa. Its Cloud MCP page returned HTTP 401 in this read, so Cloud auth details were not independently checked. |
| 3 | USD and glTF are relevant interchange formats; Blender documents USD import/export and Khronos maintains the Blender glTF importer/exporter. | 3.3 (2/1/5/5/5) | Continue using existing manifest registration and pinned glTF validation. Add a USD/glTF route only when an acceptance case requires a missing format transformation. | [Blender USD manual](https://docs.blender.org/manual/en/5.2/files/import_export/usd.html) and [Khronos glTF-Blender-IO](https://github.com/KhronosGroup/glTF-Blender-IO) were fetched with Exa. The repo already validates registered glTF assets; neither source justifies another bundled add-on by itself. |
| 4 | The two ComfyUI↔Blender projects offer artist-facing workflows, but overlap with the pod's separate ComfyUI and Blender engines. | 3.0 (3/2/3/3/4) | Keep them as UX/prototype references; if a user-facing desktop bridge is prioritized, test only a small non-production Blender workflow and compare against existing `artist3d` tools. | [ComfyUI-Blender](https://github.com/alexisrolland/ComfyUI-Blender) says it pairs Blender add-on UI with ComfyUI custom nodes and API-format workflow export; its repository states GPL-3.0. [BlenderAI-node](https://github.com/AIGODLIKE/ComfyUI-BlenderAI-node) also identifies itself as a ComfyUI-backed Blender integration and states GPL-3.0. Pages fetched with Exa; compatibility and code safety were not audited. |
| 5 | Blender's official MCP project exposes Blender through a separate MCP server and an in-Blender add-on. | 2.6 (2/3/3/1/4) | Keep its tool inventory as an idea source; implement any needed operation as a typed, allowlisted Studio tool instead of exposing arbitrary Python through MCP. | The [Blender Projects source page](https://projects.blender.org/lab/blender_mcp) describes an add-on/server architecture and Python-API access. This review did not run it. Treat it as privileged code execution, not a security boundary. |
| 6 | LTXVideo and VideoHelperSuite are common video custom-node resources, but ComfyUI core and this pod already have LTX/video paths. | 2.0 (2/1/3/2/2) | Retain as troubleshooting/version references only; check the engine manifest and selected image before considering a new node pack. | [Lightricks/ComfyUI-LTXVideo](https://github.com/Lightricks/ComfyUI-LTXVideo) was fetched with Exa; its README describes LTX-2 integration and reports license “Other.” No code/package review was performed. |

## Decision

The high-value result is the searchable, provenance-preserving catalog itself.
It expands future builder queries without growing the image or enabling new
runtime permissions. The catalog does **not** establish a new production
capability, close an acceptance item, or justify installing hundreds of
extensions. Before any software adoption, inspect its exact license, immutable
source/version, dependency tree, network and filesystem permissions, Blender or
ComfyUI compatibility, resource cost and operation-specific headless smoke.

For 3D/MCP candidates, compare with the existing role-scoped Blender tools and
the pinned extension snapshot. For model packs, verify whether the engine is
already in ComfyUI core or the pod image. Paid APIs require an explicit provider
adapter with data-egress, spend, timeout and cancellation controls. Keep all
resource budgets host-derived; rows' VRAM estimates are unverified hints, not
profiles to hardcode.
