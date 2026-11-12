# Ultra Non-GPU Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Satisfy every documented pre-GPU blocker for all mandatory capabilities and all 58 selected engines, leaving only real-GPU measurements and held-out GPU evaluation.

**Architecture:** Close each engine and product scenario against the existing typed engine, asset, provenance, orchestration, and delivery contracts. Preserve exact upstream/component provenance and require CPU-verifiable entrypoints and image wiring before a capability can advance. Keep GPU-only evidence separate and pending.

**Tech Stack:** Python/FastAPI/LangGraph, ComfyUI workflows and adapters, Blender, Remotion, FFmpeg, SQLite, Rust `comfyctl`, Docker/GitHub Actions.

**Spec:** [`2026-11-12-ultra-non-gpu-closure-design.md`](../specs/2026-11-12-ultra-non-gpu-closure-design.md), [`upgrades-ultra-version/acceptance.md`](../../../upgrades-ultra-version/acceptance.md), [`docs/ultra/CAPABILITY_EVIDENCE.md`](../../ultra/CAPABILITY_EVIDENCE.md).

## Global Constraints

- Keep all 58 currently selected engines selected and all 34 capabilities mandatory.
- Never promote a stub, proxy, mock, or unverified claim to a real implementation.
- A07 fields require per-component source, tracked lock bytes, terms/notices, real entrypoint, and build/runtime evidence.
- A CPU-only check never establishes GPU quality, CUDA compatibility, VRAM, performance, L3, or L4.
- No Modal, paid inference, private media upload, or secret-bearing artifact.
- Update `docs/ultra/status.json` and `CAPABILITY_EVIDENCE.md` only from evidence on the current main commit.

## Review Focus

- An immutable tag name is not a 40-character code revision.
- A SHA-256 string is not lock evidence unless it matches a tracked lock file and its contents describe the pinned artifacts.
- A license decision must cover code, bundled dependencies, weights, data, and optional components separately.
- A recipe name or ComfyUI template is not an engine implementation unless the actual run path validates inputs and dispatches the pinned workflow.
- Simulated outputs, synthetic fixtures, and partial L1/L2 evidence never complete a full C criterion.
- A demo can pass a CPU harness while failing its real product acceptance or provenance requirements.

---

### Task 1: Rebaseline the closure ledger on current main

**Files:** `docs/ultra/status.json`, `docs/ultra/CAPABILITY_EVIDENCE.md`, `scripts/check_engines_manifest.py` output, current CI/image evidence.

- [ ] Record current main SHA and required workflow/image digest from authoritative GitHub state.
- [ ] Recompute A07 blockers and per-capability levels from the current files; label stale snapshot claims as historical.
- [ ] Map every still-open acceptance item A00–A29, C01–C34, and D01–D05 to its current evidence and next action.
- [ ] Confirm all 58 selected IDs are present exactly once and remain in scope.

### Task 2: Close the core/platform A07 engine partition

**Engines:** `ffmpeg`, `whisperx`, `qwen3-vl`, `mission-compiler`, `production-memory`, `reference-local`, `hyperframes`, `remotion`, `blender`, `opencode-ui`, `opentimelineio`, `opencolorio`, `atlas-offline`, `research-lab`, `llm-server`, `chrono`.

- [ ] For every engine, pin source to an immutable SHA and record its exact selected component/version.
- [ ] Add tracked lock files with content hashes and component/build dependency records; validate them with the A07 checker.
- [ ] Add source-backed redistribution decisions and preserve required notices in the image.
- [ ] Implement or verify the real registered entrypoint, tests, build wiring, runtime inventory, and offline behavior for the declared scope.
- [ ] Preserve explicit differences between proxies (for example `chrono`) and real upstream engines.

### Task 3: Close the 3D/reconstruction A07 engine partition

**Engines:** `depth-anything3`, `trellis2`, `partcrafter`, `skintokens`, `sam3d-body`, `gvhmr`, `hy-motion`, `wan-move`, `scene-optimizer`, `v-rgbx`, `generative-omnimatte`, `void`, `qwen-image-layered`, `map-anything`, `tapip3d`, `diffreg-pbir`, `mitsuba`, `motionbricks`, `audio2face3d`, `sea-raft`.

- [ ] Verify each upstream revision, dependency and model/asset license separately.
- [ ] Add hash-verified locks, notices, real adapters/entrypoints, image wiring, and CPU contract tests for all listed routes.
- [ ] Reconcile shared versions (including Mitsuba/Dr.Jit) and ensure engine scopes match the implemented algorithms.
- [ ] Keep gated/restricted weights explicitly unavailable until their documented grant is present.

### Task 4: Close the video-generation A07 engine partition

**Engines:** `wan22`, `wan-animate`, `infinitetalk`, `ovow`, `alayaworld`, `ltx25`, `vace`, `diffusion-renderer`, `id-v2v`, `videomama`, `flashvsr-plus`, `seedvr2`, `storymem`.

- [ ] Verify exact code revisions, models, dependency locks, split licenses, notices, and optional/gated assets.
- [ ] Replace placeholder template bindings with real recipe/adapter contracts and implement each selected workflow's entrypoint.
- [ ] Add CPU fixtures for graph contracts, temporal/mask handling, output manifests and failure states.
- [ ] Bake only reviewed code/dependencies; keep task weights out of the image unless the reviewed lock authorizes them.

### Task 5: Close the audio, speech, image, evaluation A07 partition

**Engines:** `sam-audio`, `mmaudio`, `ace-step`, `qwen3-tts`, `qwen-image21`, `optuna`, `videoalign`, `controlfoley`, `vbench2`.

- [ ] Pin each source and dependency graph; write the per-code/per-weight redistribution and consent review.
- [ ] Implement and register real entrypoints, including build-time-only dependency acquisition where required.
- [ ] Add CPU contract tests for stems, Spanish text/audio inputs, constraints, evaluation dimensions and typed missing-weight results.
- [ ] Verify image inventory, license notices, and offline boot for all selected components.

### Task 6: Close A05/A06/A08/A09 and the autonomous director path

**Capabilities/scenarios:** C01–C04, C23–C24, C27, C31–C34, D05; acceptance A05, A06, A08, A09, A23–A26.

- [ ] Verify durable LangGraph checkpoint/resume, idempotent real recipe dispatch, lineage invalidation and persistent budgets across process restart.
- [ ] Exercise configurable provider profiles with distinct IDs/capabilities against the approved provider and contract fixtures; keep API cost bounded and explicit.
- [ ] Complete the mixed 100-asset corpus, evidence-timestamp retrieval, corrupt isolation, reference analysis, adaptation-to-project, and local no-network flow.
- [ ] Finish the real browser/client path (create, upload, plan, preview, edit, cancel, resume) with deterministic CPU backend and accessible progress/failure states.
- [ ] Complete preregistered experiments, held-out split/leakage checks, accepted-run provenance, adapter rollback, and all D05 artifacts.

### Task 7: Close Atlas, 3D, body/motion, causal, light and 4D acceptance

**Capabilities/scenarios:** C05–C22, C28–C30, D01–D04; acceptance A11–A19.

- [ ] Complete exact-view/offline Atlas selection and stratified source-validity acceptance.
- [ ] Complete project-owned GLB/GLTF geometry, rig, articulation, motion, physics, face and temporal-scene entrypoints with reopenable assets and CPU ground truth.
- [ ] Complete causal actor-on/off, mask/alpha, shadow/reflection and sound preservation fixtures; finish separate relighting and restyling contracts.
- [ ] Complete A→B→A memory drift baseline and editable 4D reconstruction/reframe path.
- [ ] Deliver D01–D04 CPU artifacts and report exactly which neural quality measures are GPU-only.

### Task 8: Close audio, Spanish voice and reproducible master delivery

**Capabilities/scenarios:** C25–C32 and D01/D04/D05; acceptance A20–A22.

- [ ] Complete scene/action sound maps, source/license checks, stem validation, and causal Foley removal fixtures.
- [ ] Complete Spanish pronunciation, alignment, speaker/turn, lexicon correction, and audio output checks against expected text.
- [ ] Verify all editability, aspect ratio, frame rate/timebase, color/range, audio loudness/true peak, captions/safe areas, and provenance on actual CPU-rendered masters.
- [ ] Produce D01/D04/D05 integrated edit packages with manifests, hashes, and intermediates.

### Task 9: Final current-main pre-GPU audit and PR sequence

- [ ] Run A07 manifest validation and independent physical-file checks; require zero non-GPU release blockers.
- [ ] Run relevant CPU integration suites and repository validation; preserve explicit pending GPU results.
- [ ] Build and smoke the current candidate image, verify its digest and all selected engine artifacts, and merge only with required CI green and branch current.
- [ ] Audit A00–A28, C01–C34 and D01–D05 against current artifacts; leave only A29 real target-GPU measurements/quality/holdouts pending.
