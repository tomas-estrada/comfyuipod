# Capability Closure Audit and Agent Brief

Audit baseline: `main` at `81f5bf39bb048f586ea3c25a045e568af85c07da` (2026-11-16).

## Current decision

The project is **not ready for production closure**. The current main workflow passed on run [43796325868](https://github.com/tomas-estrada/comfyuipod/actions/runs/43796325868), and PR #405 passed on run [43783405523](https://github.com/tomas-estrada/comfyuipod/actions/runs/43783405523). Those runs establish software and candidate-image checks, not capability completion or target-GPU performance.

The current manifest checker reports 88 engines, 57 selected, no structural coherence problems, 228 release blockers, and `release_ready: false`. The blockers affect 46 selected engines. A plain `--check` currently exits successfully despite the blockers, and CI does not yet make readiness a required production-promotion condition. This is a gate defect, not evidence that the blockers can be waived.

The capability ledger and `status.json` are stale relative to this main SHA. Recent merges #404 and #405 add CPU evidence for sound design, delivery, ASR and related flows; they do not close all non-GPU acceptance. Preserve partial capability levels until the specified acceptance evidence exists. The only acceptable final state for this goal is that all non-GPU evidence and release gates are closed; target-hardware measurements may remain explicitly identified as the only outstanding items.

## Required working rules

- Use an isolated branch/worktree per independent change and the repository agent harness for delegated work.
- Read `AGENTS.md`, relevant acceptance criteria, and the internal RAG before implementation. Treat external catalogs as leads, then verify against source, tests and pinned dependencies.
- Prefer production-route CPU integration fixtures and real artifacts. A fake collaborator, manifest declaration, synthetic fixture, or passing unit test alone does not prove engine integration or perceptual quality.
- Keep GPU measurements, held-out quality, real provider behavior, and commercial/license decisions distinct. Never fabricate measurements, consent, redistributability, or evidence.
- Run the focused suite while developing, then integrate all compatible changes and run the broad repository/CI-equivalent checks together. Recheck the exact final commit and workflow before reporting completion.
- Do not promote a release image while strict A07 readiness is false. Candidate PR builds must remain available so fixes can be validated.

## Parallel work tracks

| Track | Scope | Acceptance evidence |
|---|---|---|
| A07 release gate | Manifest checker CLI, CI workflow and promotion job only | Strict readiness mode fails on a coherent manifest with blockers and passes a clean fixture; promotion is gated while PR candidate builds remain usable; current 228 blockers remain visible and are not “fixed” by metadata-only edits. |
| Temporal scene | Project-scoped temporal-scene storage/routes/tests | A production-path CPU fixture records known object/pose/camera observations, separates train and held-out frames, persists/reloads, exercises reframe/retime, and compares A→B→A against a no-memory baseline. Unknown/inferred values stay explicitly inferred. |
| Causal audio | `scene_sound` render/route/tests | Real synthetic WAV fixtures pass through project route and FFmpeg; deleting one declared cause removes only its stem, preserves unrelated events, and emitted sample rate/clipping/loudness/true-peak values come from rendered bytes. |
| Delivery policy | EDL/Remotion schema and focused tests | Trace one undeclared or dropped policy end-to-end (color primaries/transfer or title-safe area); verify the final artifact using ffprobe and decoded pixels. Color range is not a color-space conversion. |

The current delegated work is scoped to these four tracks. Do not overlap edits across tracks; report the branch, commit, harness run ID, exact tests and known limits.

## Remaining closure sequence after this tranche

1. Review each branch and harness log; reject metadata-only claims and unproven behavior.
2. Integrate the isolated commits on a branch based on the current `main`, preserving #404/#405 and all newer main code.
3. Rebaseline the capability ledger and machine-readable status to the verified SHA, workflow, blocker count and artifact digests; leave uncertainty explicit.
4. Use one broad validation pass covering Python/Studio, Rust, repository validation, candidate build, full-stack and Studio-profile smoke. Fix all failures before opening/updating the PR.
5. Continue A00–A28, active capabilities, D01–D05, and every selected-engine blocker from the acceptance documents. The ledger must show criterion-level evidence, not only an item status.
6. Only when every non-GPU criterion is proven should the report say GPU measurement is the sole remaining class. Revalidate current main and image provenance after merge; do not claim production-ready GPU quality without real target-hardware results.

## Current baseline facts to preserve

- Main SHA: `81f5bf39bb048f586ea3c25a045e568af85c07da`.
- Manifest: 88 total / 57 selected / 228 release blockers / 46 affected selected engines / coherent / not release-ready.
- Latest main workflow evidence: run `43796325868` successful. Latest cited merged PR evidence: #405, run `43783405523` successful.
- Do not assume that the historical `candidate_image_current` entry in `status.json` describes this SHA; obtain the current run's candidate and promoted image digests before recording them.
- No GPU run or paid Modal audit is authorized or needed for this closure tranche.
