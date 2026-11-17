# C09 glTF Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pinned, offline Khronos glTF 2.0 validation report to project-owned GLB manifest registration.

**Architecture:** Keep the current Python structural preflight and add the official npm `gltf-validator` package in a minimal, separately locked Node project. A tiny Node wrapper reads one project-owned GLB, calls `validateBytes` without an external-resource callback, returns a deterministic bounded summary, and Python rejects validator errors before storing a manifest.

**Tech Stack:** Python 3.11+, Node 22, npm lockfile v3, Khronos glTF-Validator 2.0.0-dev.3.10 (Apache-2.0), existing ProjectStore and FastAPI threadpool.

**Spec:** [`upgrades-ultra-version/acceptance.md`](../../../upgrades-ultra-version/acceptance.md) A12; [`docs/ultra/CAPABILITY_EVIDENCE.md`](../../ultra/CAPABILITY_EVIDENCE.md) C09.

## Global Constraints

- Run only on a project-owned `.glb` path already resolved by `ProjectStore`.
- Never supply an external-resource callback; do not fetch network or filesystem URI references from glTF.
- Pin exact npm version and lockfile integrity; image build and CI both use `npm ci`.
- Limit input to 512 MiB, validator wall time to 30 seconds, reported issues to 50, and emitted JSON to 256 KiB.
- Preserve Apache-2.0 license and identify the upstream version and artifact SHA-512 integrity.
- Keep the current structural GLB preflight; the validator supplements it and does not raise C09 beyond its current evidence level.

## Review Focus

- Missing Node/package must fail registration clearly and must not persist a manifest.
- Validator errors must reject the manifest, while warnings remain visible in the persisted report.
- A GLB containing external URI references must not cause HTTP or outside-path reads.
- Timeout, non-JSON output, nonzero exit without a valid report, oversized input, and oversized reports fail closed.
- The report must omit timestamps and absolute paths and must redact/cap attacker-controlled fields.

---

### Task 1: Pin the standalone validator package

**Files:**
- Create: `studio/gltf-validator/package.json`
- Create: `studio/gltf-validator/package-lock.json`
- Create: `studio/gltf-validator/validate.cjs`
- Modify: `Dockerfile`
- Modify: `.github/workflows/docker-publish.yml`

**Interface:** `validate.cjs <owned-glb-path>` writes one deterministic JSON result to stdout and exits nonzero if invocation fails.

- [ ] Add a test asserting manifest output contains a versioned deterministic `gltf_validation` report.
- [ ] Run that test and confirm it fails because the report is absent.
- [ ] Pin npm package exactly at `2.0.0-dev.3.10`; commit npm integrity lock; add the no-external-read Node wrapper.
- [ ] Install with `npm ci` in Docker and CI, with the CI test environment pointing to `studio/gltf-validator`.
- [ ] Run package-lock-only npm validation and inspect the upstream license file.

### Task 2: Gate manifest registration on the report

**Files:**
- Create: `studio/comfy_studio/gltf_validation.py`
- Modify: `studio/comfy_studio/asset3d_manifest.py`
- Test: `studio/tests/test_asset3d_manifest.py`

**Interface:** `validate_glb(path: Path, *, project_root: Path, asset_sha256: str) -> dict[str, Any]` returns an asset-hash-bound, deterministic report or raises `Asset3DError`.

- [ ] Add failing tests for validator errors, missing package, and warnings retained in the record.
- [ ] Run those tests to confirm the expected pre-integration failure.
- [ ] Implement size/ownership checks, subprocess timeout and report validation; invoke after Python structural preflight and before persisting metadata.
- [ ] Ensure error reports reject registration, warning-only reports persist, and returned report has no timestamp or absolute path.
- [ ] Run focused manifest tests and Ruff.

### Task 3: Verify offline and hostile-input behavior

**Files:**
- Test: `studio/tests/test_asset3d_manifest.py`
- Modify: `docs/ultra/CAPABILITY_EVIDENCE.md`

- [ ] Exercise a valid GLB with the actual official validator on the CI runner.
- [ ] Add malformed asset, external URI, timeout, oversized input, and malformed-output regressions.
- [ ] Run the full Studio suite, repository validation, and image candidate smoke.
- [ ] Update C09 evidence conservatively: report validation is L1 contract/test evidence; do not claim 3D quality or GPU validation.
