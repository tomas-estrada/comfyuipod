# Ultra non-GPU closure design

## Intent

Bring every mandatory capability and every selected engine in the Ultra ledger
to its documented pre-GPU acceptance level. The only allowed remaining work is
the real target-GPU campaign defined by A29. This design preserves all 58
`build_selected` engines, all 34 mandatory capabilities, and demonstrations
D01–D05.

## Goals

- Verify every claim against current source, lock files, notices, runtime
  entrypoints, image contents, CPU fixtures, and production-path behavior.
- Implement missing real adapters and user-visible workflows; a manifest flag,
  stub, catalog entry, or passing mock alone never closes a capability.
- Keep `gpu_validation` pending until a real GPU run supplies measured evidence.
- Refresh the status ledger only from current-main commits, CI runs, image
  digests, and inspectable artifacts.

## Non-goals and anti-goals

- Do not use Modal, paid model inference, or another billable service for this
  phase.
- Do not narrow the selected engine set, remove mandatory capabilities, or
  reinterpret partial L1/L2 evidence as full acceptance.
- Do not fabricate source revisions, lock hashes, license decisions, notices,
  generated media, or GPU measurements.

## Constraints

- [`upgrades-ultra-version/acceptance.md`](../../../upgrades-ultra-version/acceptance.md)
  is the acceptance contract for A00–A29 and D01–D05.
- [`upgrades-ultra-version/engines.json`](../../../upgrades-ultra-version/engines.json)
  and `capabilities.json` remain the authoritative selected-engine and
  capability inventories.
- Every selected engine needs an immutable upstream revision, a tracked and
  hash-verified dependency/artifact lock, a component-level redistribution
  decision and notices review, and a real registered entrypoint included in
  the image or explicitly prepared runtime.
- CPU fixtures establish software behavior and wiring only; they do not prove
  GPU quality, CUDA compatibility, throughput, VRAM use, or L3/L4.
- External media stays synthetic, public-license, or explicitly authorized;
  private assets and credentials never enter CI artifacts.

## Approaches considered

1. **Mark manifest rows complete in batches.** Fast, but it would only hide the
   documented gaps and violate the evidence rules.
2. **Replace the engine set with a smaller runnable subset.** Easier to build,
   but it conflicts with the user's requirement and the manifest's mandatory
   capability inventory.
3. **Close the existing scope in evidence-backed slices.** For each selected
   engine, pin and review its exact component, add the actual adapter/build
   path and CPU contract, then connect the affected acceptance scenarios and
   refresh the ledger. This is the chosen approach because it preserves scope
   and keeps each claim auditable.

## Implementation decisions

- Work from current `origin/main` in isolated branches and merge tested PRs in
  small, dependency-aware tranches.
- Reuse existing Studio Engine contracts, ComfyUI transport, Blender/Remotion
  paths, dataset harness, and project-owned assets where they match acceptance.
- Require a typed failure when an entrypoint, exact model, license grant, or
  capability is unavailable. Missing hardware is `pending_gpu`; missing code,
  evidence, or wiring is still a non-GPU blocker.
- Recompute the final state from the current repository and CI; do not copy
  historical `status.json` claims forward.

## Completion proof

Completion requires a requirement-by-requirement audit showing all A00–A28,
C01–C34, A29 preparation, and D01–D05 pre-GPU criteria satisfied on current
main, all selected-engine A07 blockers resolved, a current promoted image
verified, and only actual target-GPU measurements/quality/holdouts pending.
