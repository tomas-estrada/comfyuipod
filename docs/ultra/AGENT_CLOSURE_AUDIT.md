# Capability Closure Audit and Agent Brief

Snapshot date: 2026-11-17 UTC. Verified main SHA: `38b05270787f956c5c9a7b0690e9dc8cd58ba415`.

## Decision

The project is **not production-ready**. PR #409 is merged at `38b05270787f956c5c9a7b0690e9dc8cd58ba415`. Main workflow [44008741469](https://github.com/tomas-estrada/comfyuipod/actions/runs/44008741469) passed all three jobs: repository/Python/panel checks, Rust checks, candidate build and both full-stack smoke paths. Candidate `ghcr.io/tomas-estrada/comfyuipod:sha-38b0527` has manifest digest `sha256:315f52b4f3c5544b75c9245d89d4d28a326042cdeb8c1381757b77a53f6c62ca`. It is not promoted to `latest`.

The A07 gate is active. In the main workflow it reported 233 blockers across 48 of 57 selected engines and skipped `latest`/semver promotion. The same-commit local checker reports 228 blockers across 46 engines. This disagreement is an open correctness issue; establish which manifest/input differs and make the check deterministic before using the number as a closure target. Do not conceal or “fix” the discrepancy with metadata edits or by shrinking the selected roster.

PR #408 adds bounded CPU evidence for A19 temporal-scene, causal audio and BT.709 EDL delivery, and establishes the release-promotion guard. PR #409 adds persisted mission-budget accounting and terminal behavior, conservative camera-motion confidence gating, and synthetic D03 articulated-product evidence. These changes do not satisfy all capability acceptance. A green repository CI is not proof of real provider connectivity, model quality, authorized-media quality, broad holdout evaluation, all engine adapter readiness, or target-GPU performance. The only acceptable end state for this goal is that all documented non-GPU criteria are verified closed and real-GPU measurements are the sole remaining gap.

## Merged work in this tranche

PR #409 is merged. Its branch passed required CI-01/CI-03 and Rust checks, plus candidate-image build, full-stack smoke, and `MODEL_PROFILE=studio` smoke on run 44004462639; the post-merge main workflow 44008741469 passed all three jobs. Local validation passed 279 related Python tests with 1 skipped, Ruff, and `git diff --check`.

| Workstream | Merged change | Evidence and remaining limit |
|---|---|---|
| Production planner | Persistent mission spend, idempotent attempt accounting, mission cap, typed budget terminal state, and v2 checkpoint migration | Six focused tests plus orchestration/restart suites pass. Real provider behavior and full configured LangGraph production-route acceptance remain open. |
| Audiovisual CPU | Camera-motion confidence now requires adequate field coverage and rejects ambiguous dominant subject motion | Deterministic synthetic CPU tests pass; real-footage holdout accuracy remains open. |
| 3D/motion CPU | D03 fixture adds forward kinematics, camera projection, held-out coverage, independent synthetic ground truth, and report artifact | Demo tests pass; synthetic CPU evidence only, with no render/GPU/general reconstruction claim. |
| A07 evidence | Mission-compiler lock hashes refreshed for modified orchestration files | Main CI still reports 233 blockers/48 engines; a same-commit local checker reports 228/46. The mismatch is unresolved. |

Do not elevate capability levels based only on synthetic fixtures where acceptance requires authorized real-world or held-out data. The attempted Command Code reviews did not produce usable independent findings; they are not counted as review approval.

## Required closure order

1. Reconcile the A07 CI/local blocker-count mismatch on the exact same commit and source inputs.
2. Resolve all selected engine blockers with authentic pins, dependency locks, notices, redistribution decisions, actual adapters and image inventory evidence. Preserve the selected roster unless an explicit product decision changes it; never claim support from metadata alone.
3. Close the durable production plan/execution path, project memory/retrieval and provider contract/error/privacy behavior through the configured LangGraph runtime.
4. Close media ingest, reference analysis, semantic retrieval, ASR/audio provenance, temporal scene, 3D camera/geometry, physics/rig/motion and D01-D05 CPU acceptance to the exact criteria in `upgrades-ultra-version/acceptance.md`.
5. Complete authorized real-media holdouts where the acceptance requires them. Synthetic deterministic fixtures cannot replace those results.
6. Refresh `CAPABILITY_EVIDENCE.md` and `status.json` only from merged code and current CI/image evidence. Keep `latest` promotion gated while A07 is blocked.
7. Only after every non-GPU gap is closed should the report say target-GPU measurements are the sole remaining work. Then run the authorized target-hardware plan and capture immutable image digest, telemetry, artifacts, seeds, holdouts, and failure/cost records.

## Current hard limits

- No target-GPU run was part of PR #408, PR #409, or workflow 44008741469.
- No new image was promoted to `latest`; A07 reported 233 blockers across 48 of 57 selected engines and skipped promotion.
- Historical GPU/profile results remain attached to their original images and do not transfer to this candidate.
- No paid Modal run or external provider/API call is implied or authorized by local contracts/tests.
