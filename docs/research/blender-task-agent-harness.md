# Staged, role-based Blender task harness

## Evidence used

This note adapts a user-provided ChatDev 2.0 YAML example. The example is a
design reference only; it is not an instruction to install ChatDev2, Gemini, or
the `blender-mcp` package. Its useful structure separates a product
specification, technical planning, geometry construction, look development,
and reviews. An orchestrator routes between explicit project states and sends
feedback to the responsible worker.

The current repository also has a version- and platform-pinned Blender
Extensions catalogue and an `artist3d` role that may search it. See
[`blender-extensions-integration.md`](blender-extensions-integration.md).
Local RAG is the first retrieval step for future implementation work; catalog
metadata and a design example are leads, not runtime capability evidence.

## Patterns worth adopting

1. **Give each stage one job.** Convert a short request into a measurable scene
   brief; turn that into an ordered task plan; build geometry; review geometry;
   apply materials and lighting; review the presentation. The geometry builder
   should not silently change the design, and look development should not
   rewrite approved geometry.
2. **Pass artifacts, not an unbounded transcript.** Each phase should emit a
   validated, versioned object: a scene brief, geometry task list, scene
   manifest, review findings, and final asset record. Include object IDs,
   dimensions, transforms, dependencies, constraints, and acceptance checks.
3. **Use explicit workflow state.** Route from typed status and review results,
   not a keyword embedded in free-form prose. Define allowed transitions,
   retry caps, terminal states, and the exact artifact restored after a reject.
4. **Make reviews independent and layered.** First run deterministic checks for
   object presence, bounds, transforms, scale, intersections, normals, and
   export validity. Then use a visual review for framing, material intent,
   and lighting. A screenshot cannot establish geometry correctness by itself.
5. **Restrict tools by stage.** Planning gets read-only discovery and planning
   tools. Geometry gets bounded geometry operations. Look development gets
   material and lighting operations. Reviewers get read-only scene inspection,
   metrics, renders, and findings. The existing role-restricted `ToolBelt`
   is the enforcement point; hiding a tool from a prompt is not sufficient.
6. **Use safe rollback.** Keep immutable checkpoints and restore the last
   validated scene on rejection. Do not ask a worker to delete the original
   scene and recreate it as a recovery strategy.
7. **Keep context and runtime budgets per phase.** Pass compact structured
   artifacts, cap worker iterations and wall time, and preserve provenance for
   model/provider selection, tool calls, add-on hashes, Blender version, and
   outputs. A model alias or YAML context-window number is not proof that a
   runtime enforces that limit.
8. **Ask the user only for decisions that are actually missing.** Produce a
   complete first proposal with visible assumptions, request clarification
   when an ambiguity changes the outcome, and request a final human decision
   at the review point that is intended to be user-facing.

## Proposed phase contract

| Phase | Input | Output / gate | Tool access |
|---|---|---|---|
| Brief | User intent and project assets | Scene brief with scale, style, camera, constraints, assumptions | Project read, asset search |
| Plan | Approved brief | Ordered geometry and lookdev tasks with stable object IDs and measurable checks | Planning and extension discovery |
| Geometry | Geometry tasks | Checkpointed scene plus object manifest; deterministic geometry checks pass | Typed geometry operations only |
| Geometry review | Scene checkpoint and geometry report | Structured approve/reject findings with object IDs and metric evidence | Read-only inspection, metrics, preview render |
| Look development | Approved geometry checkpoint and materials brief | Material/light changes plus provenance | Typed material, lighting, and approved add-on capabilities |
| Final review | Candidate scene, review brief, metrics and previews | Structured decision and user-ready artifact | Read-only inspection, metrics, preview |
| Delivery | Approved candidate | Blend/GLB and evidence manifest with hashes | Export and artifact registration |

The current `artist3d` role already handles 3D manifest registration, camera
previews, and catalogue discovery. It does not yet enforce this complete
geometry-versus-lookdev state machine. Treat the table as a target contract,
not a claim that all stages are implemented.

## Limits and failure modes in the example

- The YAML names one preview model throughout and uses several `context_window`
  numbers, but does not define model fallback, enforcement semantics, token
  accounting, or per-stage budgets.
- The role prompts rely on arbitrary `blender-mcp` tools for Python execution.
  A prompt rule such as “geometry only” does not constrain that server. Use
  typed, allowlisted operations and apply process-level time, memory, filesystem,
  network, and output limits.
- Free-form routing strings can be missing, duplicated, or included in quoted
  content. Use schema-validated state transitions instead.
- Some instructions conflict: the geometry reviewer is told not to care about
  appearance but is also told to delete objects; several workers require
  `bpy.context.space_data`, which is a UI context and may not exist in a
  background render.
- Visual-only inspection misses exact scale, object count, contact, topology,
  and export defects. Pair screenshots with deterministic checks.
- A fatal lookdev error should restore the last approved geometry checkpoint,
  not trigger an improvised destructive rebuild.
- Extension discovery must remain separate from execution. Search may return
  hundreds of candidates; a Blender phase can use only packages in its reviewed
  immutable lock with operation-specific smoke evidence.

## Implementation sequence

1. Keep the current safe 3D tools and add-on search available through the
   role-restricted harness.
2. Define Pydantic schemas for the scene brief, object manifest, phase plan,
   findings, and workflow state.
3. Implement the geometry review gate and durable checkpoint/restore before
   adding free-form orchestration.
4. Introduce separately scoped geometry and lookdev capabilities with tests
   proving each role is denied tools from the other phase.
5. Admit an add-on only after its exact archive hash, Blender compatibility,
   license, permission declarations, headless operation smoke, resource
   limits, and acceptance mapping are recorded.
6. Measure review agreement, repair attempts, invalid exports, latency, and
   artifact quality on representative tasks before changing defaults.

## Sources

- User-provided YAML example: `3D_builder_orchestrator_final` (conversation
  attachment). The YAML itself is treated as design input, not executable
  policy.
- Repository implementation: `studio/comfy_studio/agent/tools.py`,
  `studio/comfy_studio/engines/blender.py`, and the Blender Extensions
  integration note linked above.
