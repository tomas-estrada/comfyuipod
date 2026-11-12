# Blender Bullet CPU physics: project implementation guidance

## What the fixture proves

`scripts/smoke_blender_physics.py` runs two real headless Blender CPU scenarios:

1. Free-fall with a passive ground collider through `BlenderPhysicsSpec` and the Studio adapter. It exports evaluated world-space positions, finite-difference linear/angular velocities, unit/timebase/solver metadata, proximity-based contact events, an animated GLB and a state-hashed JSON trace.
2. A pendulum link joined to a fixed anchor by a native Blender `HINGE` constraint. The smoke records 73 frames, tests the configured Z-angle limits, resets to frame 1 and confirms the initial pose reproduces, then exports an animated GLB and a separate joint/trajectory trace.

The local CPU run on Blender 5.3.0 measured a maximum hinge excursion of about 15.0006° with a configured ±15° range and a reset-pose delta of 0 m. The image candidate repeats these tests on the image's pinned Blender 5.2.4; only a green candidate build establishes that version's result.

## Runtime and evidence rules

- Hinge permits rotation only around the constraint object's local Z axis. Align that axis deliberately; Blender's manual calls out that hinge differs from other single-axis constraints that use X.
- Configure the constraint Empty separately from the two rigid-body objects, with explicit object1/object2 and enabled limits.
- Export solver observations and constraint configuration together. Keep frame numbers, seconds, metric units, axis, limit radians/degrees, object IDs, reset result, seed and state hash.
- Contact in the current free-fall adapter is nearest-mesh proximity sampled after Bullet steps, not a Bullet manifold. Label it as proximity; never claim collision impulses or exact contact geometry.
- The user-facing Studio physics tool currently exposes free-fall for project-owned GLB/GLTF inputs. The hinge test is a pinned-image CPU fixture, not yet an agent-facing articulated-scene workflow.
- CPU fixtures establish deterministic software behavior only. They do not establish GPU throughput, motion quality, real-world physical fidelity, Chrono parity, or complete A14/C18 acceptance.

## Primary sources

- [Blender 5.2 hinge constraint manual](https://docs.blender.org/manual/en/5.2/physics/rigid_body/constraints/types/hinge.html): one rotational degree of freedom around the host constraint object's local Z axis and optional Z-angle limits.
- [Blender RigidBodyConstraint API](https://docs.blender.org/api/current/bpy.types.RigidBodyConstraint.html): object references, enabled flag, solver iterations, and angular-limit properties.
- [Blender rigid-body operators API](https://docs.blender.org/api/5.2/bpy.ops.rigidbody.html): creating constraints, worlds, and rigid bodies from Python.
