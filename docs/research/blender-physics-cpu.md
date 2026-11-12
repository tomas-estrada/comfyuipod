# Blender Bullet CPU physics: project implementation guidance

## What the fixture proves

`scripts/smoke_blender_physics.py` runs two real headless Blender CPU scenarios:

1. Free-fall with a passive ground collider through `BlenderPhysicsSpec` and the Studio adapter. It exports evaluated world-space positions, finite-difference linear/angular velocities, unit/timebase/solver metadata, proximity-based contact events, an animated GLB and a state-hashed JSON trace.
2. A pendulum link joined to a fixed anchor by a native Blender `HINGE` constraint. The smoke records 73 frames, tests the configured Z-angle limits, resets to frame 1 and confirms the initial pose reproduces, then exports an animated GLB and a separate joint/trajectory trace.

The local CPU run on Blender 5.3.0 measured a maximum hinge excursion of about 15.0006° with a configured ±15° range and a reset-pose delta of 0 m. Merged PR #332 repeats both tests on the pinned Blender 5.2.4 image; its Rust, repository-validation, and candidate-image smoke jobs passed in [run 43143151199](https://github.com/tomas-estrada/comfyuipod/actions/runs/43143151199). The full Studio suite also passed after fixing the test-stub signature.

## Runtime and evidence rules

- Hinge permits rotation only around the constraint object's local Z axis. Align that axis deliberately; Blender's manual calls out that hinge differs from other single-axis constraints that use X.
- Configure the constraint Empty separately from the two rigid-body objects, with explicit object1/object2 and enabled limits.
- Export solver observations and constraint configuration together. Keep frame numbers, seconds, metric units, axis, limit radians/degrees, object IDs, reset result, seed and state hash.
- Contact in the current free-fall adapter is nearest-mesh proximity sampled after Bullet steps, not a Bullet manifold. Label it as proximity; never claim collision impulses or exact contact geometry.
- Merged PR #332 exposes both bounded free-fall and hinge maneuvers through the role-restricted Studio artist3d simulate_physics tool for project-owned GLB/GLTF assets. It stores an animated GLB and JSON trace as project assets. This is a connected CPU workflow, not full W12 articulated-scene or contact-physics acceptance.
- CPU fixtures establish deterministic software behavior only. They do not establish GPU throughput, motion quality, real-world physical fidelity, Chrono parity, or complete A14/C18 acceptance.

## Inverse-rendering smoke

The same merged PR adds an optional pinned Mitsuba 3.9.1 scalar CPU fixture. It recovers a synthetic albedo of 0.7 as 0.699998; parameter MSE drops from 0.36 to 3.14e-12 and held-out-view MSE from 0.4065 to 3.66e-12. This proves that the fixture can fit and evaluate a known synthetic scene. It does not provide a Studio agent tool, real-capture material reconstruction, differentiable-gradient validation, GPU performance, or full C19/W16 acceptance.

## Primary sources

- [Blender 5.2 hinge constraint manual](https://docs.blender.org/manual/en/5.2/physics/rigid_body/constraints/types/hinge.html): one rotational degree of freedom around the host constraint object's local Z axis and optional Z-angle limits.
- [Blender RigidBodyConstraint API](https://docs.blender.org/api/current/bpy.types.RigidBodyConstraint.html): object references, enabled flag, solver iterations, and angular-limit properties.
- [Blender rigid-body operators API](https://docs.blender.org/api/5.2/bpy.ops.rigidbody.html): creating constraints, worlds, and rigid bodies from Python.
