# D02 CPU Media Evidence Implementation Plan

> **For agentic workers:** This is a single testable slice; use TDD and keep any claim of evidence scoped to synthetic CPU fixtures.

**Goal:** Make D02 emit and verify actual before/after RGB images and PCM audio that demonstrate causal-layer removal and ambient preservation.

**Architecture:** Use the existing fixture's small known-ground-truth pixel buffers and sound-event intervals to generate deterministic PNG and WAV artifacts with the standard library. Independently inspect the written media in tests, record artifact hashes and measured image/audio errors in the D02 report, and include all media in run-manifest and resume validation.

**Tech Stack:** Python standard library (`struct`, `zlib`, `wave`, `hashlib`), existing pytest and Pillow test dependency.

**Spec:** `upgrades-ultra-version/acceptance.md` (D02 causal removal and A25 reproducible scenarios).

## Global Constraints

- Preserve strict fixture validation and existing six D02 invariants.
- Store outputs only under the contained per-demo output directory; include every file in manifest SHA-256 and resume checks.
- Use deterministic synthetic CPU fixtures only; claim no neural quality, real-footage quality, or GPU evidence.
- Add no runtime dependencies or external service calls.

## Review Focus

- Actor, shadow, and reflection regions must change in the PNG while intact regions remain byte/pixel-identical.
- Actor-caused audio must be removed from the WAV while the ambient reference remains within the declared exact synthetic tolerance.
- Invalid image dimensions, event timing, or sample values must fail closed rather than create misleading evidence.
- Manifest resume must reject a modified PNG or WAV.
- Reports must identify synthetic CPU scope and report measured values separately from declared event metadata.

### Task 1: Write and verify failing D02 media acceptance tests

**Files:**
- Modify: `studio/tests/test_demos.py`
- Test against: `studio/comfy_studio/demos.py` and `studio/comfy_studio/data/demos/d02_causal_layers.json`

- [x] Assert `run_demo("D02")` writes before/after PNG and WAV artifacts, that decoded pixels show all three causal regions removed and intact pixels preserved, and that audio output removes actor energy while matching ambient samples.
- [x] Assert report and run-manifest SHA-256/byte records cover all media, scope is `synthetic_cpu_only`, `gpu_used` is false, and resume rejects a tampered output.
- [x] Run the focused tests and confirm they fail because the media outputs were absent.

### Task 2: Implement deterministic media outputs and measurements

**Files:**
- Modify: `studio/comfy_studio/demos.py`
- Modify: `studio/comfy_studio/data/demos/d02_causal_layers.json`
- Test: `studio/tests/test_demos.py`

- [x] Generate valid RGB PNGs and 16-bit PCM WAVs from validated fixture data; measure the written files and attach the report records.
- [x] Add all D02 files to safe-output checks, expected artifacts, manifest hashes, and resume verification.
- [x] Run D02-focused tests, the full `test_demos.py`, Ruff, and Python compile checks; preserve all six existing invariants.

