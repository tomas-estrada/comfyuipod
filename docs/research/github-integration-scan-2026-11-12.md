# Public GitHub integration scan: fast ComfyUIPod upgrades

**Snapshot:** 2026-11-12. This is a targeted search across the current Studio
agent/MCP harness, ComfyUI workflow stack, Blender/3D pipeline, media quality
and review UX. GitHub search is not exhaustive, so this is a reproducible
shortlist rather than a claim to enumerate every related public repository.

## Method and scoring

Repository discovery used the connected GitHub repository-search tool and
parallel OpenCode Go `glm-5.3-flash` read-only agents. A follow-up dispatched
four concurrent, non-overlapping domain searches for agent deployment and
resource use, Blender/3D, ComfyUI workflows, and media pipelines. Agents
inspected the pod first, then used GitHub public repository search/API and
README/license files. No repositories were cloned, installed, or modified.
Searches covered ComfyUI MCP and workflows, Blender MCP/skills/headless
automation, agent security/sandbox and tracing, video scene detection/quality
evaluation, 3D validation/optimization, context compression, deployment, and
review-player UX.

Each eligible candidate receives six evidence-informed ratings from 0–5:

`score = 0.25 × fit + 0.20 × integration speed + 0.15 × novelty + 0.15 × capability impact + 0.15 × maintenance + 0.10 × security/privacy fit`.

Stars are deliberately excluded. Fit measures overlap with current open ledger
gaps and code. Speed penalizes new daemons, heavy dependencies, and runtime
requirements. Novelty is relative to already-present ComfyUI/Blender engines,
LangGraph, VBench, FFmpeg checks, MCP server, and the repo's internal agent
harness. Missing licensing evidence is an adoption blocker and receives no
score. These ratings make the ranking repeatable; the inputs still require
engineering judgment and are not measured performance results.

## Ranked candidates

Subscores are `fit / speed / novelty / impact / maintenance / security`.

| Rank | Repository | Scores | Total | Integration opportunity and decision |
|---:|---|---:|---:|---|
| 1 | [KhronosGroup/glTF-Validator](https://github.com/KhronosGroup/glTF-Validator) | 4 / 4 / 4 / 4 / 5 / 5 | **4.25** | Add its standards-based report beside `studio/comfy_studio/asset3d_manifest.py`. The repo pins the official [npm package](https://www.npmjs.com/package/gltf-validator) `gltf-validator@2.0.0-dev.3.10` (Apache-2.0, zero runtime dependencies, 383,347 unpacked bytes, SHA-512 integrity in `studio/gltf-validator/package-lock.json`). A bounded local Node wrapper validates only bytes from the project-owned GLB and has no external-resource resolver. The [upstream release](https://github.com/KhronosGroup/glTF-Validator/releases/tag/2.0.0-dev.3.10) is a pre-release last published in 2024, despite newer upstream source activity; it is pinned exactly and remains replaceable. |
| 2 | [Netflix/vmaf](https://github.com/Netflix/vmaf) | 4 / 3 / 3 / 4 / 5 / 5 | **3.90** | Potential perceptual metric for `engines/qa.py` and delivery checks. It needs an FFmpeg build with `libvmaf` and a valid reference signal; VBench already exists in a separate environment, so this is useful only for reference-based comparisons that current FFmpeg checks cannot cover. |
| 3 | [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect) | 5 / 3 / 2 / 3 / 5 / 4 | **3.75** | CPU cut detection for reference analysis, highlights, or montage. Existing `_scene_cut_times` already uses FFmpeg, lowering novelty. OpenCV dependency size and changed boundary semantics must be measured before replacement. |
| 4 | [snyk/agent-scan](https://github.com/snyk/agent-scan) | 3 / 4 / 4 / 3 / 3 / 5 | **3.55** | Possible CI scan for external skills/MCP tool definitions before they enter a lock or runtime. Confirm the current license and whether signatures/rules work offline before making it a release gate; scanner output is detection evidence, not enforcement. |
| 5 | [arjun988/blender-skills](https://github.com/arjun988/blender-skills) | 3 / 5 / 2 / 2 / 4 / 4 | **3.35** | MIT prompt/playbook material could inform selected typed Blender actions without adding a runtime dependency. Adapt recipes to the pod's role-restricted ToolBelt; skill text is not an execution boundary or quality guarantee. |
| 5 | [videojs/video.js](https://github.com/videojs/video.js) | 4 / 3 / 2 / 3 / 4 / 4 | **3.35** | Self-hosted player option for review pins, EDL ranges, and subtitles in the panel. Verify the exact v10 license and bundle size, then compare against the existing native `<video>` flow before adopting. |
| 7 | [PrefectHQ/fastmcp](https://github.com/PrefectHQ/fastmcp) | 4 / 3 / 2 / 3 / 5 / 2 | **3.30** | Could replace parts of `studio/comfy_studio/mcp_server.py` if SSE/resources become a real requirement. Current server intentionally has no new dependency, is already authenticated/redacted, and covers the pod's needed subset; defer the framework until a missing MCP capability is specified. |
| 7 | [squall01337/mixamo-llm-mocap](https://github.com/squall01337/mixamo-llm-mocap) | 4 / 1 / 4 / 5 / 3 / 3 | **3.30** | Staged headless Blender pose/retarget workflow is a possible C12–C14 reference. It requires GPU-backed pose inference and its declared license metadata needs verification; it is a later GPU-campaign candidate, not a pre-GPU quick win. |
| 9 | [superradcompany/microsandbox](https://github.com/superradcompany/microsandbox) | 3 / 1 / 3 / 4 / 3 / 5 | **2.95** | Linux-first microVM boundary for untrusted workloads could strengthen engine isolation beyond subprocesses. High operational cost and unverified macOS parity make this a production-pod experiment, not an immediate integration. |
| 10 | [mem0ai/mem0](https://github.com/mem0ai/mem0) | 3 / 3 / 1 / 2 / 4 / 3 | **2.70** | Searchable memory service overlaps `studio/comfy_studio/agent_brain/`; only benchmark it if the existing retrieval path misses a measured recall target. Avoid running two memory systems without evidence. |
| 11 | [shulkyn-dev/3d-model-remesher](https://github.com/shulkyn-dev/3d-model-remesher) | 4 / 2 / 2 / 3 / 1 / 4 | **2.65** | Headless mesh LOD/retopology idea for Hunyuan3D outputs, but the project had no recent activity in the inspected metadata. Port a small validated Blender operation only if a concrete export failure justifies it. |
| 11 | [HuginStudios/usd_chunks](https://github.com/HuginStudios/usd_chunks) | 3 / 2 / 4 / 3 / 1 / 3 | **2.65** | Re-cookable procedural USD networks could inform later scene lineage, but the repository labels itself alpha and says it is not production-ready. Reference only. |
| 11 | [agentgateway/agentgateway](https://github.com/agentgateway/agentgateway) | 2 / 2 / 3 / 2 / 4 / 4 | **2.65** | MCP policy proxy may help if the pod starts connecting additional MCP servers. None are configured in `opencode.json`; introducing a gateway now solves no active runtime gap. |
| 14 | [comet-ml/opik](https://github.com/comet-ml/opik) | 2 / 2 / 1 / 3 / 5 / 2 | **2.45** | Mature tracing/evaluation platform, but a full self-hosted deployment adds substantial services and overlaps durable `.agent-runs`/checkpoint evidence. Reconsider if trace-based evals become a specific acceptance requirement. |

## Follow-up discovery: additional candidates

This second pass uses the same six dimensions and formula above, and only adds
repositories not already scored in the first shortlist. Repository metadata
and `pushed_at` were checked against the linked GitHub API endpoints on
2026-11-12. Scores are project-specific estimates, not benchmark results.

| Rank among additions | Repository | Scores | Total | Incremental fit and disposition |
|---:|---|---:|---:|---|
| 1 | [zeux/meshoptimizer](https://github.com/zeux/meshoptimizer) · [metadata](https://api.github.com/repos/zeux/meshoptimizer) | 4.5 / 3.5 / 3.5 / 4 / 4.5 / 4.5 | **4.08** | MIT; pushed 2026-10-14. An optional GLB optimization stage can reduce asset size after validation. Start with a CPU fixture and preserve the original; compare byte size and geometry/material invariants. Estimated 1–3 days. |
| 2 | [microsoft/LLMLingua](https://github.com/microsoft/LLMLingua) · [metadata](https://api.github.com/repos/microsoft/LLMLingua) | 3.5 / 3.5 / 3.5 / 3.5 / 4.5 / 3.5 | **3.65** | MIT; pushed 2026-11-09. Adds model-based prompt compression, but overlaps the existing memory/context work and is already in the proposal catalog. Benchmark only on representative sessions; preserve protected instructions and evidence verbatim, and measure quality, latency and memory. Do not enable globally. |
| 3 | [WyattBlue/auto-editor](https://github.com/WyattBlue/auto-editor) · [metadata](https://api.github.com/repos/WyattBlue/auto-editor) | 3.5 / 4 / 3 / 3.8 / 4 / 3.5 | **3.65** | Unlicense; active 2026-11-12. A quick adapter could turn silence-based edits into a reviewable EDL. It overlaps FFmpeg/EDL and needs a reversible-import spike before any render path uses it. |
| 4 | [modelcontextprotocol/python-sdk](https://github.com/modelcontextprotocol/python-sdk) · [metadata](https://api.github.com/repos/modelcontextprotocol/python-sdk) | 3.5 / 3.5 / 2.5 / 3.5 / 4.8 / 4 | **3.60** | Official MIT SDK; pushed 2026-11-05. Use for protocol-conformance tests against the custom local MCP endpoint first. Migrate only if those tests expose a concrete maintenance or interoperability gap; keep the existing auth, redaction and tool allowlist. |
| 5 | [microsoft/TRELLIS.2](https://github.com/microsoft/TRELLIS.2) · [metadata](https://api.github.com/repos/microsoft/TRELLIS.2) | 4 / 1.5 / 4.5 / 5 / 3.5 / 2.5 | **3.50** | GitHub reports MIT and a 2026-07-10 push. New 3D-generation capability, but CUDA/model dependencies, memory profile, license terms for model artifacts, asset provenance and quality evaluation make this a later opt-in engine experiment rather than a fast MVP. |
| 6 | [Comfy-Org/comfy-mcp](https://github.com/Comfy-Org/comfy-mcp) · [metadata](https://api.github.com/repos/Comfy-Org/comfy-mcp) | 4.5 / 2.5 / 1.5 / 4 / 4 / 2 | **3.25** | Useful as a feature benchmark for node/model discovery, workflow validation and queue lifecycle, but Studio already owns these seams. GitHub metadata has no SPDX identifier; the repository describes AGPL-3.0-or-later or a commercial license. Do not add a second server or copy code before license review. |
| 7 | [dstackai/dstack](https://github.com/dstackai/dstack) · [metadata](https://api.github.com/repos/dstackai/dstack) | 2.5 / 2 / 3.5 / 4 / 4.5 / 3.5 | **3.18** | MPL-2.0; active 2026-11-12. Unified scheduling for GPU clouds, VMs and clusters could matter if deployment grows to multiple pods. It adds a control plane without solving today's single-pod dynamic-memory or Vast deployment gap. Defer until there is a multi-pod acceptance requirement. |

The existing first-pass score for [Breakthrough/PySceneDetect](https://github.com/Breakthrough/PySceneDetect)
remains unchanged; it is already in the first ranking and the proposal catalog.
The follow-up verified its source was pushed on 2026-11-10, but did not find
evidence that it is integrated in the active FFmpeg keyframe path. Compare it on
a labeled CPU fixture before deciding whether its additional dependency is
worth keeping.

## Excluded or deferred

Other near matches were not promoted: `SYSTRAN/faster-whisper` is already part
of the Studio ASR design; `ggml-org/whisper.cpp` is a possible CPU fallback but
duplicates the existing transcription chain; `Tencent-Hunyuan/Hunyuan3D-2.1`
overlaps the opt-in Hunyuan path and GitHub reports no SPDX license and an older
2025-10-17 push; `shaka-packager` adds HLS/DASH packaging beyond the current
single-file delivery path and has no SPDX identifier; `BAVC/qctools` is a
desktop-oriented QC application with missing SPDX metadata; and
`URUWorks/TeroSubtitler` is an active MPL-2.0 desktop editor that overlaps
Studio's subtitle editing/export rather than adding a clear headless operation.

- [LingyiChen-AI/comfyui-workflow-skill](https://github.com/LingyiChen-AI/comfyui-workflow-skill): README advertises 360+ node definitions, but no license was declared and its automatic model downloads conflict with the manifest-gated acquisition policy. Do not vendor it. Existing `LangGraph`, curated templates, and graph validation reduce the novelty of its natural-language planner.
- [ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) and [Blender Foundation MCP](https://projects.blender.org/lab/blender_mcp): useful feature maps, but both expose Blender's Python API to model-written actions. That crosses the repo's no-arbitrary-code boundary; keep typed, allowlisted operations in the existing subprocess runner.
- [sandraschi/blender-mcp](https://github.com/sandraschi/blender-mcp): 41 headless tools can inspire new typed operations, but a second MCP/`uvx` daemon duplicates the current engine contract and adds runtime dependencies.
- [LangWatch](https://github.com/langwatch/langwatch) was found, but overlaps Opik's tracing/eval role. [vidstack/player](https://github.com/vidstack/player) is deprecated in favor of Video.js v10 and was excluded.
- Blender marketplace/cloud tools such as Meshy, Scenario, Morphic, and hosted model APIs require accounts, network, or paid operations. They are not eligible for default pod-image integration without an explicit user transfer/billing decision and a constrained provider adapter.

## First implementation candidate

The first connected Parallel Decisions comparison selected **glTF-Validator**
with probability 0.852 and confidence 0.823. After verifying current package
metadata, a later comparison selected the minimal npm integration with
probability 0.774 and confidence 0.662; both are prioritization signals, not
ground truth. The weighted ranking pointed to a useful uncovered path: add a
reproducible, report-producing validation step for project-owned 3D assets,
without a new agent service or GPU requirement. The project now pins the
official package and exact SHA-512 integrity, installs it in a minimal lockfile
project in the image and CI, and records a deterministic report when registering
project-owned GLBs. The wrapper omits timestamps and absolute paths, bounds
input/time/output, rejects validator errors, preserves warnings, and never
resolves external URIs. CPU fixtures exercise valid and semantically invalid
GLBs, external-resource non-fetching, missing package, timeout, and bounded
report persistence. This raises validation coverage only; it does not establish
3D quality, reconstruction fidelity, or GPU performance.

## Evidence boundary

Repository descriptions, license fields, README claims, and `pushed_at` dates
came from GitHub public APIs and repository files inspected on 2026-11-12.
Maintenance scores use license/activity signals and inspected CI/release notes
where available; they are not a security audit or proof that an add-on works in
this pod. CPU fixtures can validate deterministic software behavior only. The
Mixamo/pose, VMAF, player and microVM candidates still need target-environment
smokes before they can be accepted as capabilities.
