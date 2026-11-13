# Ultra Non-GPU Acceptance Closure Plan

> **For agentic workers:** execute each independent task as a harnessed OpenCode run; preserve this plan and the project run record.

**Goal:** Close every documented non-GPU acceptance gap in `upgrades-ultra-version/acceptance.md`, `docs/ultra/CAPABILITY_EVIDENCE.md`, and `docs/ultra/status.json`; leave only genuine target-GPU measurements and held-out GPU quality evidence pending.

**Architecture:** Treat the acceptance matrix and capability ledger as requirements, not as evidence. Implement and connect CPU-capable product paths where possible; keep neural/model and GPU-dependent steps explicitly pending. Resolve A07 engine blockers only with real source pins, reproducible dependency locks, reviewed redistribution/notices, executable adapters, and image evidence.

**Tech Stack:** Existing FastAPI/ProjectStore, Python, FFmpeg, Pillow, Blender headless where available, SQLite, Rust supervisor, Docker build and repository CI.

**Spec:** `upgrades-ultra-version/acceptance.md`, `docs/ultra/CAPABILITY_EVIDENCE.md`, `docs/ultra/status.json`, and `upgrades-ultra-version/engines.json`.

## Global Constraints

- Do not use Modal, its inference endpoint, or any other paid inference/API for this work.
- Never infer product readiness from a mock, symbolic contract, manifest row, or green CI alone.
- Do not claim GPU execution, GPU quality, target-GPU compatibility, or L3/L4 without measured artifacts from the target hardware.
- Keep CPU and unavailable-model states explicit; runtime must not download engines or weights at boot.
- Preserve project ownership, path containment, bounded processing, source hashes, and provenance in every media route.
- Use the repository agent harness; keep tasks non-overlapping and record verification in the run artifact.

## Review Focus

- Project and asset IDs from another project must fail closed.
- Retries must be idempotent and conflicting writes must not silently replace saved work.
- Invalid/non-finite timestamps, positions, color values, sizes, and media must fail with actionable errors.
- CPU output must be inspected as an actual artifact; metadata-only evidence cannot claim rendering or matting.
- Missing models, binaries, and GPU must remain visible as unavailable/pending rather than simulated success.

## Parallel implementation tranche

### Task 1: Persistent temporal scene state (C06/C17, A19)

Files: `studio/comfy_studio/spatial_memory.py`, `studio/comfy_studio/routers/spatial_memory.py`, focused spatial-memory tests.

Add an editable, bounded time-indexed object/pose/camera representation with strict timebase validation, deterministic serialization, reframe/retime operations, source provenance, project-scoped persistence, and a synthetic A→B→A/held-out CPU fixture. Do not call this neural 4D reconstruction.

### Task 2: Render supplied causal audio stems (C25, A20)

Files: `studio/comfy_studio/audio.py`, `studio/comfy_studio/routers/audio.py`, focused audio tests.

Extend event metadata to refer to project-owned source samples and implement deterministic FFmpeg assembly with onset/timebase alignment, bounded decoding, separate stems, measured sample rate/clipping/loudness/true peak, and causal deletion that preserves unrelated events. No generated or model-separated audio claim.

### Task 3: User-directed chroma-key alpha sequence (C29/A17)

Files: new focused module/route under `studio/comfy_studio/`, router registration, focused tests.

Implement bounded CPU chroma-key matting for an explicitly selected flat background color, producing real RGBA frames or alpha-bearing video and provenance. Reject malformed inputs and keep the feature named chroma-key; do not claim semantic/general video matting or model quality.

### Task 4: Project-persisted retarget workflow (C11-C14, A15)

Files: new `studio/comfy_studio/routers/motion.py`, router registration, focused motion-route tests; reuse `studio/comfy_studio/motion.py` contracts.

Expose bounded project-owned skeleton/clip save, validation, retarget, and export/import through Studio. Preserve root path, timebase, identity/action/mask/camera channels, contacts, limits, and provenance. Do not claim neural motion generation or performance capture.

Each task must first add a failing focused test, implement the minimum real path, run its own focused tests, report exact artifact/evidence and remaining acceptance gaps, and not edit A07 manifests or the other tasks' files.

## Remaining work after this tranche

Re-run the current acceptance audit. Continue with every remaining C01-C34/A00-A28/D01-D05 requirement and all 56 currently incomplete selected A07 engine entries. Reconcile status only from current checks and artifacts. The separate GPU campaign is outside this plan and remains the only permitted final class of pending evidence.
