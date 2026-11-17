# Capability Closure Audit and Agent Brief

Snapshot date: 2026-11-17 UTC. Authoritative main SHA: `a1f847df6d270aed512a725b4705cc2a2089b768`.

## Decision

The project is **not production-ready**. PR #408 is merged and post-merge workflow [43983976860](https://github.com/tomas-estrada/comfyuipod/actions/runs/43983976860) passed all three jobs: repository/Python/panel checks, Rust checks, candidate build and both full-stack smoke paths. Candidate `ghcr.io/tomas-estrada/comfyuipod:sha-a1f847d` has manifest digest `sha256:76ebe67fdb7d98d7a78ee5280b4dda7c11d10a33202b0f2e7e5ede1c98c4a9b7`. It is not promoted to `latest`.

The A07 gate is active. In the main workflow it reported 233 blockers across 48 of 57 selected engines and skipped `latest`/semver promotion. The same-commit local checker reports 228 blockers across 46 engines. This disagreement is an open correctness issue; establish which manifest/input differs and make the check deterministic before using the number as a closure target. Do not conceal or “fix” the discrepancy with metadata edits or by shrinking the selected roster.

PR #408 adds bounded CPU evidence for A19 temporal-scene, causal audio and BT.709 EDL delivery, and establishes the release-promotion guard. These changes do not satisfy all capability acceptance. A green repository CI is not proof of real provider connectivity, model quality, authorized-media quality, broad holdout evaluation, all engine adapter readiness, or target-GPU performance. The only acceptable end state for this goal is that all documented non-GPU criteria are verified closed and real-GPU measurements are the sole remaining gap.

## Active workstreams

Current local integration branch: `codex/closure-ledger-refresh-20261117`, based on main `a1f847df6d270aed512a725b4705cc2a2089b768`. Three independent changes are integrated locally and pass focused CPU checks; they are not yet committed, published, or CI-verified. The previous delegation status in this document was stale and has been replaced with this actual state.

| Workstream | Scope | Integration evidence |
|---|---|---|
| Workstream | Local change | Evidence and limit |
|---|---|---|
| Production planner | Persistent mission spend, idempotent attempt accounting, remaining mission cap, typed budget terminal state and v2 checkpoint migration | Six focused tests pass; configured-provider behavior and full production-route acceptance remain open |
| Audiovisual CPU | Camera-motion estimator now gates confidence on field coverage and rejects ambiguous dominant subject motion | 34 focused tests pass on deterministic synthetic clips; real-footage holdout accuracy remains open |
| 3D/motion CPU | D03 articulated-product fixture adds FK/projection, held-out coverage, independent synthetic ground truth and a report artifact | 53 demo tests pass; synthetic CPU evidence only, no render/GPU/general reconstruction claim |
| A07 release gate | Updated mission-compiler lock hashes for the two modified orchestration source files | Local checker remains 228 blockers/46 engines. CI on main `a1f847d` reported 233/48; the five-blocker mismatch remains unresolved |

Integrated validation on this branch: 279 related Python tests passed, 1 skipped; Ruff passed, `git diff --check` passed. No PR CI or candidate image has run for this branch. Do not raise capability levels from synthetic fixtures where acceptance requires real-world or held-out data. Agent reviews attempted via `scripts/cmd-agent` could not connect to the model API; local review remains necessary.

## Required closure order

1. Reconcile the A07 CI/local blocker-count mismatch on the exact same commit and source inputs.
2. Resolve all selected engine blockers with authentic pins, dependency locks, notices, redistribution decisions, actual adapters and image inventory evidence. Preserve the selected roster unless an explicit product decision changes it; never claim support from metadata alone.
3. Close the durable production plan/execution path, project memory/retrieval and provider contract/error/privacy behavior through the configured LangGraph runtime.
4. Close media ingest, reference analysis, semantic retrieval, ASR/audio provenance, temporal scene, 3D camera/geometry, physics/rig/motion and D01-D05 CPU acceptance to the exact criteria in `upgrades-ultra-version/acceptance.md`.
5. Complete authorized real-media holdouts where the acceptance requires them. Synthetic deterministic fixtures cannot replace those results.
6. Refresh `CAPABILITY_EVIDENCE.md` and `status.json` only from merged code and current CI/image evidence. Keep `latest` promotion gated while A07 is blocked.
7. Only after every non-GPU gap is closed should the report say target-GPU measurements are the sole remaining work. Then run the authorized target-hardware plan and capture immutable image digest, telemetry, artifacts, seeds, holdouts, and failure/cost records.

## Current hard limits

- No target-GPU run was part of PR #408 or workflow 43983976860.
- No new image was promoted to `latest`; the A07 warning is the expected release gate outcome.
- Historical GPU/profile results remain attached to their original images and do not transfer to this candidate.
- No paid Modal run or external provider/API call is implied or authorized by local contracts/tests.
