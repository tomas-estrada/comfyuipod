# Mission compiler

`mission-compiler` is a CPU-only, deterministic compiler from a versioned creative brief to a bounded task DAG. It validates the brief, preserves evidence references and explicit constraints, detects invalid dependencies and cycles, enforces input/task/cost bounds, and returns a stable compilation receipt. The orchestrator projects that result into the existing `ProductionState`; it does not add a second persistence authority or call an external service.

The execution path is `ProductionService.plan(..., brief=...)` through `comfy_studio.mission_compiler.compile_mission`. It remains subject to the existing human storyboard approval gate before execution. The implementation is covered by `studio/tests/test_mission_compiler.py`; focused verification also exercises the production service and graph.

The compiler is original repository code covered by the root MIT license and introduces no third-party package or model. Runtime dependencies used by the surrounding Studio application remain pinned in `studio/pyproject.toml`. Source and lock evidence are recorded in `upgrades-ultra-version/engines.json` and `upgrades-ultra-version/locks/mission-compiler.lock.json`.

This CPU implementation does not claim that the full C03 or C08 capability acceptance is complete, and it makes no GPU performance or render-quality claim.
