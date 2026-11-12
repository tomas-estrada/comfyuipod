# Model-connected extensions and tools for existing pod engines

Research snapshot: 2026-11-11. Product features, API model names, prices, and
compatibility can change. Recheck official source pages before pinning a
dependency or making a paid request.

## Scope and repository fit

The pod already has ComfyUI, a local llama.cpp llm_server, Blender,
Capture3D/asset acquisition, audio and voice engines, and workflow tools. Its
Studio agent uses an OpenAI-compatible client and role-restricted ToolBelt.
The goal is to connect selected model services to those engines without adding
a second unrestricted agent or installing many add-ons into every image.

The current Blender extension catalogue is searchable by the artist3d role
offline and returns version/hash/license/permissions. That is candidate
discovery; extensions are not enabled by the search tool.

## Findings

| Connector | Existing engine it extends | Evidence-backed capability | Runtime fit and caveats | Recommendation |
|---|---|---|---|---|
| [Comfy MCP](https://docs.comfy.org/agent-tools/mcp) | ComfyUI | First-party local and hosted MCP. Documented tools search templates, models and nodes; inspect node schemas; validate workflows; run templates/workflows; wait, retrieve and chain outputs. | Local integration expects ComfyUI/comfy-cli and a running ComfyUI. Cloud execution requires account/OAuth or an API key and runs on hosted GPUs. A second MCP server duplicates this pod's ComfyUI client, templates, jobs and output handling. | **High fit as a feature map, not as another daemon.** Add missing read-only discovery and schema validation to Studio's existing ComfyUI adapter/ToolBelt. Keep generation behind curated templates, graph validation, job controls and artifact checks. |
| [Blender Foundation MCP](https://www.blender.org/lab/mcp-server/) | Blender | Connects an MCP client to Blender's Python API for scene inspection and actions. Current docs require Blender 5.1+. Official docs explicitly warn it executes LLM-generated code without safeguards that prevent deletion or data exfiltration. | It is a powerful code-execution bridge. The Blender add-on communicates with an MCP side process; background mode requires synchronous calls and lacks deferred responses. The pod image has a headless Blender runner but must not expose arbitrary model-written Python to project files or secrets. | **Do not ship as a production agent bridge in its current form.** It can inform isolated development/benchmark work only in a disposable, network-restricted worker. Prefer typed capabilities in the pod harness. |
| [Blender Agent](https://projects.blender.org/Rich-Siomporas/blender-agent) | Blender, LLM client, MCP clients | A community harness built on Blender Foundation MCP. Its repository describes a web UI, OpenAI-compatible chat endpoint, HTTP MCP, headless Blender startup, skills, tool extensions and context compaction. | Its docs say clients that can reach the HTTP port can run Python in Blender and advise keeping the port on loopback/trusted networks. In-browser WebGPU inference is described as experimental. Claims still need code review, lock review, and isolated operational tests before adoption. | **Study its harness patterns; don't install it into the pod.** Its typed domain extensions and context/artifact UX are useful references. Do not treat the HTTP boundary as a sandbox. |
| [Meshy REST API](https://docs.meshy.ai/en/api) | Capture3D/asset acquisition, then Blender validation/import | The API exposes task-based 3D generation and processing. Image-to-3D accepts an image URL or data URI and can return GLB among selected target formats; tasks are asynchronous and can be polled or streamed. It publishes an OpenAPI 3.1 schema. | Requires an external API key, network upload, account/billing, and task lifecycle handling. Model names and deprecations change; the current API notice lists Meshy-7 3D generation retirement on 2026-12-31. External output still needs local GLB validation, size/polycount limits, provenance, and permission review. | **Good optional provider adapter candidate.** Add behind the existing 3D acquisition contract, with per-task price/consent, cancellation, rate limits, bounded upload/output, and explicit model ID. Keep existing local Hunyuan3D path as an alternative. |
| [Scenario for Blender](https://blender.scenario.com/) | Interactive Blender authoring, asset generation, rendering | Vendor docs describe image, video, 3D, PBR materials, audio, environment, render and blockout lanes; a local MCP server can read a scene, run tools and generate. It routes among models including Meshy, Rodin, Tripo and Hunyuan. | Requires Blender 5.0+, a Scenario account, API access and network. The docs call the extension experimental; generation uses credits and surfaces price/confirmation. The workflow is primarily viewport-oriented. | **Useful optional interactive design integration**, not a headless pod dependency. If adopted, put it in a separate user-facing profile with spend confirmation and no long-lived credential inside images. |
| [LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/quick_start) | Studio LLM client and local llm_server routing | Official docs describe one OpenAI-format interface to 100+ model providers, including local Ollama/vLLM and cloud services, with virtual keys, spend tracking/budgets, and load balancing. | Adds a proxy service, database/configuration for budgets, provider credentials, network paths, and another supply-chain dependency. It routes model calls but does not make unsafe tools safe. Provider support and costs vary. | **Medium fit when multi-provider routing is a real need.** First test it as a separately deployed loopback gateway behind the existing OpenAI-compatible Studio client; pin it, restrict provider/model aliases, set spend and timeout limits, and redact request logs. Skip it for a single local llama.cpp endpoint. |
| [vLLM OpenAI-compatible server](https://docs.vllm.ai/en/stable/cli/serve/) | Studio LLM client / model-serving profile | The official server exposes OpenAI-style endpoints and supports tool calling for supported model/parser combinations. Docs expose tool-parser selection, structured tool strictness, and model selection. | Adds substantial GPU/model memory and driver requirements. Tool-call support depends on model and parser, and parser plugins are executable server-side code. A profile must fit observed GPU/host memory and not assume fixed RAM/VRAM values. | **High fit for a dedicated GPU inference profile**, not the default pod image. Connect through the existing LLM client, keep tool enforcement in Studio, and use detected resource budgets plus pinned server/model revisions. |
| Blender AI marketplace add-ons such as [Morphic](https://morphic.com/blender) and [Artificial Studio](https://app.artificialstudio.ai/plugins/blender) | Interactive Blender | Vendor pages describe viewport copilots and hosted image/video/3D generation through remote extension repositories and accounts. | Closed/hosted services, API keys or sign-in, recurring model/price changes, and limited headless evidence. Remote repository installation updates from the repository, which is not an immutable production dependency. | **Don't bake these into the pod.** Consider only as optional desktop UX after reviewing terms, costs, data egress, and support for the actual Blender build. |

## Highest-value integration path

1. **Use the current harness as the control plane.** The pod already has a
   ComfyUI adapter, prebuilt templates, async jobs, a Blender subprocess runner,
   3D manifest registration and offline extension search. A duplicate MCP
   daemon is unnecessary for those same jobs.
2. **Improve ComfyUI discovery and preflight.** Add role-scoped read-only
   search_nodes/search_models views from the live object_info and pinned
   model manifests; add schema/link validation before any custom graph runs.
   Do not make agent discovery invoke ComfyUI-Manager installs.
3. **Add provider adapters at the task boundary.** A Meshy-like service should
   create a bounded asynchronous generation task through Capture3D/asset
   acquisition, then pass its GLB through existing file checks, manifest
   registration and Blender camera previews. Never let a Blender add-on hide
   the network/billing side effect inside a scene-edit tool.
4. **Keep model serving replaceable.** The Studio LLM client should continue
   using a narrow OpenAI-compatible contract. Route to llama.cpp, LiteLLM or
   vLLM by configuration/profile, with explicit model names, tool support,
   budget and timeout limits. Model proposals do not grant tool access.
5. **Separate interactive plugins from the pod image.** Scenario/Morphic/
   Artificial Studio can be tested as opt-in artist tools. Production workers
   should consume explicit inputs/outputs through a documented API or workflow,
   not the artist's preferences database or live Blender session.

## Safety, cost, and reproducibility gates

- Discover broadly; execute narrowly. Only selected, reviewed, hash-pinned
  packages or provider adapters enter an execution profile.
- Do not treat MCP, a separate subprocess, localhost binding, or a Blender
  process as an OS sandbox. Isolate project input/output mounts, credentials,
  network egress, memory, CPU, runtime and process tree.
- Keep credentials outside container layers and logs. Send only the selected
  asset and information required by the task to a remote provider; record
  provider/model/version and any user-approved data transfer.
- For hosted models, expose price estimates and an approval point before
  billable calls. Apply quotas, cancellation, retries with idempotency, and
  provider-specific timeout ceilings.
- Validate generated 3D outputs locally (extension/MIME, archive paths, GLB
  parser, dimensions, triangle count, texture bytes, cameras and transforms)
  before opening them in Blender.
- Record each adapter's source revision, dependency hashes, model/provider,
  request/response IDs, cost where available, and output content hashes.

## Repository mapping

- studio/comfy_studio/engines/comfyui.py: node introspection, graph submit,
  install guard, and existing workflow execution.
- studio/comfy_studio/engines/llm_server.py: local llama.cpp server and
  loopback-only OpenAI-compatible completion path.
- studio/comfy_studio/asset_acquire.py, capture3d, and
  asset3d_manifest.py: candidate boundary for remote model-generated GLB
  acquisition and local validation.
- studio/comfy_studio/engines/blender.py: headless Blender engine; optional
  extension installation must remain behind a reviewed lock.
- studio/comfy_studio/agent/tools.py: role-scoped tools and existing 3D
  manifest, render-preview, and extension-search actions.

## Sources

- Blender Foundation, [MCP Server](https://www.blender.org/lab/mcp-server/),
  and [source repository](https://projects.blender.org/lab/blender_mcp).
- Comfy-Org, [Comfy MCP documentation](https://docs.comfy.org/agent-tools/mcp)
  and [source repository](https://github.com/Comfy-Org/comfy-mcp).
- Meshy, [REST API documentation](https://docs.meshy.ai/en/api) and
  [Image to 3D API](https://docs.meshy.ai/en/api/image-to-3d).
- Scenario, [Blender extension handbook](https://blender.scenario.com/).
- LiteLLM, [provider list](https://docs.litellm.ai/docs/providers) and
  [proxy quick start](https://docs.litellm.ai/docs/proxy/quick_start).
- vLLM, [server CLI reference](https://docs.vllm.ai/en/stable/cli/serve/).
- Blender Projects, [community Blender Agent repository](https://projects.blender.org/Rich-Siomporas/blender-agent).
- Vendor plugin pages for [Morphic](https://morphic.com/blender) and
  [Artificial Studio](https://app.artificialstudio.ai/plugins/blender).
