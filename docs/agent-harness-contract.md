# Universal agent execution harness

Every agent launched for this repository must receive a run contract before it
starts work and must leave a durable terminal record. This applies to Studio,
OpenCode agents and subagents, Command Code (`cmd`), and any future runtime.

The shared contract is `comfyuipod.agent-run` version 1, implemented in
`studio/comfy_studio/agent/harness.py`. It records the task hash, runtime/model
identity, parent run, role, allowed tools, iteration/LLM/context/time budgets,
checkpoints, tool evidence, and a terminal status. It deliberately excludes raw
prompts, credentials, tool arguments, and tool results. Context summaries are
lossy, so decisions and evidence must be checkpointed outside model context.

## Runtime requirements

1. Launch through a repository adapter that creates the run record and supplies
   the contract to the runtime.
2. Restrict tools at dispatch time, not only in the prompt or model schema.
3. Enforce the limits the runtime exposes. Check cancellation and wall time at
   safe boundaries; record limits the runtime cannot interrupt as advisory.
4. Persist checkpoints and a terminal status (`completed`, `failed`,
   `cancelled`, `max_iterations`, `budget_exhausted`,
   `wall_budget_exhausted`, or `interrupted`).
5. A child run inherits the parent's task identity, tool subset, and remaining
   budgets. It cannot widen them.
6. Report evidence and limitations; a clean exit alone does not prove task
   correctness.

## Enforcement boundary

Studio can enforce the tool allowlist, context bound, shared LLM-call budget,
iteration ceiling, and cooperative wall/cancel checks in its own loop. Its LLM
request cannot currently be interrupted mid-flight, and the persisted run is
project-scoped.

OpenCode and Command Code are separate runtimes. Repository instructions and
launch adapters can pass the same contract, record process exit, and require
durable evidence. They cannot intercept private tool dispatch, internal model
compaction, or a runtime-created subagent unless that runtime exposes a hook for
it. Therefore those fields must be marked `advisory` in external run records;
never claim that prompt text enforces them. An agent that bypasses its adapter
is an untracked run and does not satisfy this repository's harness requirement.

Resource sizing must be derived from the current host/runtime limits, not from
fixed RAM or VRAM profiles. The harness records limits and measurements; it does
not pretend a CPU run establishes GPU capacity or model quality.
