# Capability Closure Audit and Agent Brief

Snapshot date: 2026-11-17 UTC. Verified main SHA: `b612fbe7369dd20d66d8933095f23882ade7a2af`.

## Decision

The project is **not production-ready**. Main is `b612fbe7369dd20d66d8933095f23882ade7a2af`, including PR #413's durable production approval queue and PR #414's engine-registry RAG. Main workflow [44033581865](https://github.com/tomas-estrada/comfyuipod/actions/runs/44033581865) passed all three jobs: repository/Python/panel checks, Rust checks, candidate build, full-stack smoke and Studio-profile smoke. Candidate `ghcr.io/tomas-estrada/comfyuipod:sha-b612fbe` has manifest digest `sha256:e321fab21694bceb5b39a2027598a7daf77f300e51322c1ae739e8cd454ff996` and config digest `sha256:ef07b50043e03cdb14ff6200c59f2e675d1895bfcd02240935f5b1c965ae152c`. It is not promoted to `latest`.

The A07 gate is active. On current main, both the workflow and local checker report **228 blockers across 46 of 57 selected engines**; the manifest is coherent, but not release-ready, so `latest`/semver promotion was skipped. Workflow 44033581865 checked out full Git history (`fetch-depth: 0`). The earlier 233/48 report belongs to the older `38b0527` snapshot and is retained below as historical evidence; it is not the current main count. Preserve the selected roster and close blockers with authentic source, dependency, legal, adapter and image evidence.

PR #408 adds bounded CPU evidence for A19 temporal-scene, causal audio and BT.709 EDL delivery, and establishes the release-promotion guard. PR #409 adds persisted mission-budget accounting and terminal behavior, conservative camera-motion confidence gating, and synthetic D03 articulated-product evidence. These changes do not satisfy all capability acceptance. A green repository CI is not proof of real provider connectivity, model quality, authorized-media quality, broad holdout evaluation, all engine adapter readiness, or target-GPU performance. The only acceptable end state for this goal is that all documented non-GPU criteria are verified closed and real-GPU measurements are the sole remaining gap.

## Merged work in this tranche

PR #409 is merged. Its branch passed required CI-01/CI-03 and Rust checks, plus candidate-image build, full-stack smoke, and `MODEL_PROFILE=studio` smoke on run 44004462639; the post-merge main workflow 44008741469 passed all three jobs. Local validation passed 279 related Python tests with 1 skipped, Ruff, and `git diff --check`.

| Workstream | Merged change | Evidence and remaining limit |
|---|---|---|
| Production planner | Persistent mission spend, idempotent attempt accounting, mission cap, typed budget terminal state, and v2 checkpoint migration | Six focused tests plus orchestration/restart suites pass. Real provider behavior and full configured LangGraph production-route acceptance remain open. |
| Audiovisual CPU | Camera-motion confidence now requires adequate field coverage and rejects ambiguous dominant subject motion | Deterministic synthetic CPU tests pass; real-footage holdout accuracy remains open. |
| 3D/motion CPU | D03 fixture adds forward kinematics, camera projection, held-out coverage, independent synthetic ground truth, and report artifact | Demo tests pass; synthetic CPU evidence only, with no render/GPU/general reconstruction claim. |
| A07 evidence | Current strict gate and local checker agree on the current manifest | Run 44033581865 reports 228 blockers across 46/57 selected engines; promotion remains gated until those blockers are closed. |

Do not elevate capability levels based only on synthetic fixtures where acceptance requires authorized real-world or held-out data. The attempted Command Code reviews did not produce usable independent findings; they are not counted as review approval.

## Required closure order

1. Resolve the 228 current A07 blockers with authentic pins, dependency locks, notices, redistribution decisions, actual adapters and image inventory evidence. Preserve the selected roster; never claim support from metadata alone.
2. Close the durable production plan/execution path, project memory/retrieval and provider contract/error/privacy behavior through the configured LangGraph runtime.
3. Close media ingest, reference analysis, semantic retrieval, ASR/audio provenance, temporal scene, 3D camera/geometry, physics/rig/motion and D01-D05 CPU acceptance to the exact criteria in `upgrades-ultra-version/acceptance.md`.
4. Complete authorized real-media holdouts where the acceptance requires them. Synthetic deterministic fixtures cannot replace those results.
5. Refresh `CAPABILITY_EVIDENCE.md` and `status.json` only from merged code and current CI/image evidence. Keep `latest` promotion gated while A07 is blocked.
6. Only after every non-GPU gap is closed should the report say target-GPU measurements are the sole remaining work. Then run the authorized target-hardware plan and capture immutable image digest, telemetry, artifacts, seeds, holdouts, and failure/cost records.

## Current hard limits

- No target-GPU run was part of workflows 44008741469 or 44033581865.
- Candidate `sha-b612fbe` passed software/image smoke checks but was not promoted; current A07 reports 228 blockers across 46 of 57 selected engines.
- Historical GPU/profile results remain attached to their original images and do not transfer to this candidate.
- No paid Modal run or external provider/API call is implied or authorized by local contracts/tests.
