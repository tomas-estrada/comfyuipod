# Asset-backed camera animatic design

## Intent

Upgrade the existing `render_camera_animatic` tool so it can render a camera path through a project-owned GLB using Blender Cycles on CPU. This addresses the current synthetic top-down preview for C05/C08 and gives the operator an inspectable view of the supplied scene while keeping GPU quality claims pending.

## Goals

- Keep the existing schematic preview behavior when no `asset_id` is supplied.
- When `asset_id` is supplied, require a project-owned GLB and a valid saved Asset3D manifest; use its source transform and up-axis.
- Interpolate and validate the supplied camera path, keyframe the Blender camera, render a bounded PNG sequence on CPU, encode an MP4 with FFmpeg, and probe the result.
- Record source hash, manifest hash, camera path, render settings, and `quality_claim: false` on the output asset.
- Fail closed when Blender is absent, simulated, returns invalid frames, or the output probe fails.

## Non-goals

- No GPU rendering, neural reconstruction, factuality guarantee, or visual-quality score.
- No camera path inference; supplied coordinates remain user/agent proposals and are recorded as such.
- No external media, network fetch, or modification of the source GLB.

## Approaches

1. Keep the schematic-only renderer: least change, but does not close the documented 3D preview gap.
2. Render separate stills and stitch them: uses Blender but produces discontinuous camera cuts and does not execute the path.
3. Sample the `CameraPlan` once per output frame, insert explicit Blender camera keyframes at those integer frames, render a bounded CPU frame sequence, and encode/probe it: selected because the rendered scene follows the exact sampled path while avoiding version-sensitive F-curve editing.

The existing `BlenderRenderSpec` will gain a validated camera trajectory. Each integer render frame is an explicit location/rotation/lens keyframe, so no interpolation is delegated to Blender and the implementation uses Blender's animatable lens property. The agent tool will opt into that path with `asset_id`; its current synthetic route remains the fallback for callers without a 3D asset. CPU output remains an animatic, not a final-quality render.

## Constraints

- Keep frame, duration, pixel-frame, upload-size, subprocess-timeout, path-containment, and project-ownership checks.
- Reject simulated Blender results and malformed frame sequences.
- Preserve manifest transforms and source provenance; never describe inferred geometry as observed.
