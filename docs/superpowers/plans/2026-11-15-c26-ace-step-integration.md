# C26 ACE-Step Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make C26's directed-music entrypoint submit a bounded ACE-Step 1.5 ComfyUI workflow with pinned optional weights, report a project-owned audio output, and leave all model-quality claims pending real GPU evidence.

**Architecture:** Reuse the pinned ComfyUI core nodes and the existing template registry, agent tool belt, model manifest, on-demand acquisition, and project gallery. Keep one official AIO checkpoint optional behind a dedicated music opt-in; no runtime package install, hosted generation, stems, or unverified quality claim.

**Tech Stack:** Python 3.12, Pydantic v2, ComfyUI API workflow JSON, JSON model/engine manifests, pytest, Ruff.

**Spec:** `upgrades-ultra-version/acceptance.md` A20; `upgrades-ultra-version/backlog.json` W19; `upgrades-ultra-version/capabilities.json` C26; `upgrades-ultra-version/engines.json` ACE-Step record; pinned ComfyUI node definitions in `config/nodes.lock`.

## Global Constraints

- Capability L-levels describe demonstrated evidence; do not claim neural quality, GPU performance, stems, or production acceptance from CPU fixtures.
- Weights stay outside the base image and are acquired only on explicit opt-in through the repository's hash-pinned manifest path.
- Every model file uses immutable source revision, exact bytes, and SHA-256; do not follow a moving branch.
- Generation is submitted through the existing ComfyUI service and registered in the active project gallery.
- Do not use Modal, paid inference APIs, or a real GPU for this slice.
- Keep all tool parameters bounded; no arbitrary workflow graphs, paths, or URLs enter the agent interface.

## Review Focus

1. Music weights are absent or opt-in is disabled; tool discovery/submit must report the actionable unavailable state without pretending generation occurred.
2. Invalid duration, BPM, seed, or blank prompt is rejected before ComfyUI submission.
3. A successful submit must return an audio artifact, validate it, and register it to the requested project; a video/image-only or empty output must fail closed.
4. Lyrics/language/key are optional composition inputs; never infer a Colombian/Paisa accent or claim separately editable stems.
5. Retried or failed jobs must not duplicate a project output or report successful audio without verified output metadata.

---

### Task 1: Pin optional ACE-Step weights

**Files:**
- Modify: `config/models-studio.json`
- Modify: opt-in flag registry, acquisition selection, and storage accounting files discovered in `studio/comfy_studio/model_assets.py`, `scripts/storage_requirement.py`, `scripts/download-models.sh`, and `scripts/check_models_manifest.py`
- Test: add a focused manifest/resource test without editing the template or agent-tool tests

**Interface:** Add one optional AIO checkpoint for `Comfy-Org/ACE-Step-1.5`, immutable revision `371bb1680f8c88d8c1083f6ffb6330d3d55bff76`, path `checkpoints/ace_step_1.5_turbo_aio.safetensors`, size `10025478736`, SHA-256 `67b0f43aa5c51c840bd0228e6a935d8ff416ec87e5df2fc0637da17a561252bc`, Apache-2.0, enabled only by `STUDIO_INCLUDE_MUSIC=1`.

- [x] Added manifest/resource contracts for opt-in-only selection and the exact immutable pin.
- [x] Added the manifest entry and wired opt-in through selectors, storage accounting, CLI helper, and CI dry-run.
- [x] Ran focused manifest/resource tests and offline manifest validation.

### Task 2: Add native ACE-Step template

**Files:**
- Create: `studio/comfy_studio/templates/data/ace_step_music.json`
- Test: create a dedicated template contract test

**Interface:** Template id `ace_step_music`; inputs `prompt`, optional `lyrics`, `duration_s` (1–300), `bpm` (10–300), `timesignature`, `keyscale`, `language`, and `seed`; one audio output. Use the Comfy-Org ACE-Step 1.5 AIO graph and the pinned built-in ACE-Step nodes; model path must match Task 1.

- [x] Added template contracts for prompt, bounded parameters, checkpoint name, registered built-in nodes, and audio output.
- [x] Added the API-format workflow with explicit placeholders and output mapping.
- [x] Ran the template validator and focused template contract tests.

### Task 3: Route `generate_music` through ComfyUI

**Files:**
- Modify: `studio/comfy_studio/agent/tools.py`
- Test: create a dedicated `studio/tests/test_c26_ace_step_tool.py`

**Interface:** Keep the existing `generate_music` tool name, expand its validated args to the Task 2 parameters, and submit `ace_step_music` using the existing template registry/client path. Require a verified audio output before project registration; preserve project ownership and idempotent registration behavior.

- [x] Added CPU tests for parameter wiring, missing-weight failure, valid audio registration, and rejection of empty/non-audio outputs.
- [x] Implemented the template-backed handler and updated the live-path documentation.
- [x] Ran the new tool tests plus relevant existing tool/music tests.

### Task 4: Record engine and capability evidence

**Files:**
- Modify: `upgrades-ultra-version/engines.json` ACE-Step entry
- Modify: `docs/ultra/CAPABILITY_EVIDENCE.md` C26 row and snapshot only after Tasks 1–3 pass
- Create or update: `docs/engines/ace-step.md` if the engine docs index requires a per-engine page

**Interface:** Engine record must link the immutable ComfyUI core revision, model manifest/lock path, registry entrypoint, CPU test evidence, optional weight policy, license basis, and GPU status `not_run`. C26 may rise to L1 only after the working code path and tests above are verified; L2 requires a real prepared-engine CPU integration beyond a stub, and GPU evidence remains pending.

- [x] Recorded repository/engine-manifest validation and linked the executable evidence test.
- [x] Updated engine/capability records with evidence paths and conservative levels.
- [x] Ran manifest validation, focused C26 tests, Ruff, and the full Studio suite (3007 passed, 3 skipped).
- [x] Reviewed the diff for scope and claim accuracy; real-GPU validation remains pending.
