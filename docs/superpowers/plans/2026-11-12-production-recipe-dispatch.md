# Production Recipe Dispatch Implementation Plan

> **For agentic workers:** Use the repository's normal review and verification workflow for each task.

**Goal:** Ensure production selects only recipe workflows that match verified templates/assets and dispatches them through the registered ComfyUI adapter.

**Architecture:** Keep model-family recipe identity separate from the runtime adapter key. Mark only the two verified Wan recipe/template pairs selectable; reject mismatches and unresolved recipes before any runner call.

**Tech Stack:** Python, FastAPI, LangGraph production service, pytest.

**Spec:** `docs/ultra/status.json` and the production recipe contract in `studio/comfy_studio/av_recipes.py`.

## Global Constraints

- CPU checks establish routing and contract behavior, not GPU quality or VRAM measurements.
- Unknown recipes, unimplemented workflows, and recipe/template mismatches fail closed.
- Recipe engine identity must not be used as an adapter registry key.

## Review Focus

- Large VRAM must not make an unimplemented recipe executable: selector tests reject LTX/Qwen/finishing mismatches.
- A stale or absent aggregate binding summary must not allow `recipe_unresolved` to reach the engine: runner test rejects before graph fallback.
- A valid Wan recipe must reach `comfyui` with its exact template and preserved recipe identity.
- Explicit template binding must reject an unsupported template/recipe pairing.
- Template render parameters must exclude orchestration-only recipe metadata.

---

### Task 1: Gate recipe selection on verified runtime support

**Files:** `studio/comfy_studio/av_recipes.py`, `studio/tests/test_av_recipes.py`, `studio/comfy_studio/orchestration/recipe_binding.py`, `studio/tests/test_recipe_binding.py`

- [x] Mark the two verified Wan pairs runtime supported; leave LTX, Qwen, SeedVR2, and FlashVSR visible in the catalog but not selectable.
- [x] Make default T2V dimensions fit the verified Wan T2V recipe.
- [x] Preserve the family identity in recipe metadata and use `comfyui` as the runtime adapter key.
- [x] Verify unsupported image/upscale and 720p T2V requests remain unresolved.

### Task 2: Validate recipe identity at dispatch

**Files:** `studio/comfy_studio/orchestration/adapters.py`, `studio/tests/test_m6_orchestration_adapters.py`

- [x] Reject unresolved recipe markers before applying the default template.
- [x] Reject unknown/unsupported recipe IDs and template/engine mismatches before invoking the engine.
- [x] Verify valid Wan recipe identity and template survive to a captured ComfyUI engine job.

### Task 3: Production integration and repository verification

**Files:** `studio/tests/test_m6_production_wiring.py`, `studio/tests/test_integration_graph_executability.py`

- [x] Exercise a recipe-bound plan through production approval and fake ComfyUI transport, checking terminal output only after expected dispatch.
- [x] Verify an unresolved recipe cannot be approved or create output assets even with stale aggregate metadata.
- [ ] Run focused and full required local validation, then open a PR and wait for required CI.
