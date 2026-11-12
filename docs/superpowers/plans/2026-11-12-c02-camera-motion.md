# C02 Local Camera Motion Estimator Plan

**Goal:** Estimate approximate per-shot camera movement from authorized local videos and preserve evidence and uncertainty in the editable recipe.

**Architecture:** Decode a bounded set of tiny grayscale frames with the existing FFmpeg binary. Estimate sparse block motion in pure Python, classify only coherent global movement, and return `unknown` when texture, samples, or agreement are insufficient. Keep the current `camera_moves` list for compatibility and add explicit per-shot evidence metadata.

**Tech Stack:** Existing FFmpeg subprocess helpers, Python standard library, existing Studio dependencies only.

**Spec:** `upgrades-ultra-version/acceptance.md` A09 and `docs/ultra/CAPABILITY_EVIDENCE.md` C02.

## Constraints

- Work offline on project-owned video; do not call paid or external connectors.
- Bound frame dimensions, sample count, duration, and subprocess time.
- Keep the legacy move vocabulary `pan`, `static`, `zoom`, `unknown`.
- `unknown` is required when evidence is weak; flat/textureless frames cannot prove `static`.
- Camera classifications are inferred; retain the shot interval's existing origin separately.
- CPU synthetic fixtures prove software behavior only, not real-footage accuracy or GPU readiness.

## Review Focus

- Moving foreground objects must not be reported as camera motion: moving-patch fixture must favor the stationary background.
- Textureless and undersampled cuts must remain `unknown`.
- FFmpeg missing, failing, timing out, or returning malformed bytes must degrade to `unknown` with a limitation, not break the existing analysis route.
- Every cut must have one move and one evidence record; recipe mapping must preserve this alignment.
- Sampling and block matching must remain bounded for long videos and high cut counts.

## Tasks

1. Add unit tests for static textured frames, global translation, a moving object over a static background, zoom-like radial flow, low texture, and malformed samples. Verify red before implementation.
2. Implement the pure-Python block matcher and conservative classifier.
3. Add bounded FFmpeg frame sampling and integrate one result per cut into `analyze_reference_asset`.
4. Serialize inferred method/confidence/sample evidence and include the limitation only where movement remains unknown.
5. Run focused and required repository validation, update the C02 ledger conservatively, open a PR, and merge only after both required checks pass on an up-to-date branch.
