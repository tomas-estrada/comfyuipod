# Asset-backed camera animatic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `render_camera_animatic` render a bounded camera path through a project-owned GLB using real Blender Cycles CPU output.

**Architecture:** Add a validated camera trajectory to `BlenderRenderSpec`, keyframe Blender's camera in the existing headless runner, then encode and probe its PNG sequence with FFmpeg. Extend the tool with an optional project asset ID; no asset ID retains the existing schematic renderer. Save hash-bound source and path provenance and reject simulated/invalid engine results.

**Tech Stack:** Python, Pydantic, Blender Python API, FFmpeg/ffprobe, pytest.

**Spec:** [`2026-11-13-asset-backed-camera-animatic-design.md`](../specs/2026-11-13-asset-backed-camera-animatic-design.md)

## Global Constraints

- Blender work is CPU-only, bounded, and project-owned.
- A synthetic/stub output never counts as real Blender evidence.
- This does not claim neural quality, factual camera placement, or GPU performance.
- Preserve the existing schematic path when `asset_id` is absent.

## Review Focus

- Missing or cross-project GLB IDs fail before engine invocation.
- Missing, stale, malformed, or source-mismatched manifests fail closed.
- Non-finite, coincident, unordered, or out-of-range camera keyframes are rejected.
- Blender output count, PNG format/dimensions, FFmpeg exit, and ffprobe metadata must match the requested frame contract.
- A stubbed Blender result or absent Blender must not be saved as completed output.

---

### Task 1: Camera trajectory contract in Blender runner

**Files:**
- Modify: `studio/comfy_studio/blender.py`
- Test: `studio/tests/test_blender_output_validation.py`

- [x] Add a Blender camera trajectory keyframe dataclass with frame, position, target, and horizontal FOV, and validate finite coordinates, distinct position/target, frame range/order, and FOV.
- [x] Add failing tests for a valid trajectory script, invalid frame ordering, non-finite values, and incompatibility with still camera views.
- [x] Add `camera_trajectory` to `BlenderRenderSpec`; require multi-frame PNG Cycles CPU rendering and at least two keyframes.
- [x] Generate Blender script that applies asset transform/up-axis, creates a camera, inserts location/rotation/lens keyframes for every integer output frame, and renders the bounded animation.
- [x] Verify focused spec/script tests pass and generated script parses with `compile`.

### Task 2: Engine adapter mapping

**Files:**
- Modify: `studio/comfy_studio/engines/blender.py`
- Test: `studio/tests/test_m4_blender_engine.py`

- [x] Add an adapter mapping from a validated `camera_trajectory` job payload to `BlenderRenderSpec`.
- [x] Add failing adapter tests for valid trajectory forwarding and malformed trajectory rejection.
- [x] Run the focused Blender engine tests.

### Task 3: Asset-backed animatic tool

**Files:**
- Modify: `studio/comfy_studio/agent/tools.py`
- Modify: `studio/comfy_studio/animatic.py`
- Test: `studio/tests/test_animatic.py`
- Test: `studio/tests/test_asset3d_manifest.py`

- [x] Add optional `asset_id` to `RenderAnimaticArgs` and update the tool description with the two execution modes.
- [x] Add a helper that encodes a numbered PNG sequence into MP4 with argv-only FFmpeg and verifies stream, dimensions, duration, and decoded frame count with ffprobe.
- [x] In the tool handler, resolve only the current project's GLB and hash-validated manifest, sample the camera plan within frame/pixel-work bounds, and pass transform/up-axis to Blender.
- [x] Fail closed on Blender errors/stubs, wrong frame count, malformed PNGs, FFmpeg errors, and probe mismatch.
- [x] Persist source SHA-256, canonical manifest SHA-256, sampled camera path, FPS/duration/dimensions, engine identity, and `quality_claim: false` in output metadata.
- [x] Add tool coverage for cross-project IDs, missing manifests, successful provenance, and CPU execution; retain existing tests for invalid frames and the synthetic fallback.
- [x] Verify focused animatic/3D tests, Ruff, local Blender/FFmpeg smoke, and `git diff --check`.

### Task 4: Documentation and review

**Files:**
- Modify: `studio/comfy_studio/agent/tools.py` tool description
- Modify: `docs/ultra/CAPABILITY_EVIDENCE.md` only after current-main evidence is available

- [x] Keep C05/C08 at partial evidence and describe the new CPU scene preview without implying final render quality.
- [ ] Run required repository validation and open a PR from a branch based on current `main`.
- [ ] Merge only when the current head is up to date and all required CI/image checks pass.

## Validation record

- Focused Ruff: passed.
- Focused animatic, manifest, Blender output, and engine tests: 68 passed.
- Local Blender 5.3 Cycles CPU fixture: rendered two 160x90 PNGs; FFmpeg encoded them and ffprobe confirmed two frames, 1.0 s duration, and no audio.
- Full Studio suite: 2,974 passed, 4 skipped, 1 failed because this checkout environment lacks `langgraph`, required by `test_a05_production_langgraph_integration.py`.
