# Public GitHub integration scan: fast ComfyUIPod upgrades

**Snapshot:** 2026-11-12. This is a targeted search across the current Studio
agent/MCP harness, ComfyUI workflow stack, Blender/3D pipeline, media quality
and review UX. GitHub search is not exhaustive, so this is a reproducible
shortlist rather than a claim to enumerate every related public repository.

## Method and scoring

Repository discovery used the connected GitHub repository-search tool and
parallel OpenCode Go `glm-5.3-flash` read-only agents. Agents used `gh search
repos` plus GitHub's public repository API and raw README/license files. No
repositories were cloned, installed, or modified. Searches covered ComfyUI MCP
and workflows, Blender MCP/skills/headless automation, agent security/sandbox
and tracing, video scene detection/quality evaluation, 3D validation, and
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
| 1 | [KhronosGroup/glTF-Validator](https://github.com/KhronosGroup/glTF-Validator) | 4 / 4 / 4 / 4 / 5 / 5 | **4.25** | Add its standards-based JSON validation report beside `studio/comfy_studio/asset3d_manifest.py` for uploaded GLB/glTF. The Khronos validator complements the current structural checks. Pin the exact JS/WASM artifact and run it in a bounded local subprocess; first prove that its license, size, and image packaging fit. |
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

## Excluded or deferred

- [LingyiChen-AI/comfyui-workflow-skill](https://github.com/LingyiChen-AI/comfyui-workflow-skill): README advertises 360+ node definitions, but no license was declared and its automatic model downloads conflict with the manifest-gated acquisition policy. Do not vendor it. Existing `LangGraph`, curated templates, and graph validation reduce the novelty of its natural-language planner.
- [ahujasid/mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) and [Blender Foundation MCP](https://projects.blender.org/lab/blender_mcp): useful feature maps, but both expose Blender's Python API to model-written actions. That crosses the repo's no-arbitrary-code boundary; keep typed, allowlisted operations in the existing subprocess runner.
- [sandraschi/blender-mcp](https://github.com/sandraschi/blender-mcp): 41 headless tools can inspire new typed operations, but a second MCP/`uvx` daemon duplicates the current engine contract and adds runtime dependencies.
- [LangWatch](https://github.com/langwatch/langwatch) was found, but overlaps Opik's tracing/eval role. [vidstack/player](https://github.com/vidstack/player) is deprecated in favor of Video.js v10 and was excluded.
- Blender marketplace/cloud tools such as Meshy, Scenario, Morphic, and hosted model APIs require accounts, network, or paid operations. They are not eligible for default pod-image integration without an explicit user transfer/billing decision and a constrained provider adapter.

## First implementation candidate

The connected Parallel Decisions comparison selected **glTF-Validator** with
probability 0.852 and reported confidence 0.823. This aligns with the weighted
score and an existing uncovered path: add a reproducible, report-producing
validation step for project-owned 3D assets, without a new agent service or
GPU requirement. The selection is a prioritization signal, not proof that the
binary, license, or resource profile is already acceptable. Next implementation
must verify the exact release/license, immutable artifact pin, bounded subprocess
behavior, and malformed/valid GLB fixtures before changing the C09 evidence.

## Evidence boundary

Repository descriptions, license fields, README claims, and `pushed_at` dates
came from GitHub public APIs and repository files inspected on 2026-11-12.
Maintenance scores use license/activity signals and inspected CI/release notes
where available; they are not a security audit or proof that an add-on works in
this pod. CPU fixtures can validate deterministic software behavior only. The
Mixamo/pose, VMAF, player and microVM candidates still need target-environment
smokes before they can be accepted as capabilities.
