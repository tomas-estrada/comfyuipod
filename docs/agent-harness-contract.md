# Universal agent execution harness

Every agent launched for this repository must receive a run contract before it
starts work and must leave a durable terminal record. This applies to Studio,
OpenCode primary agents and subagents, Command Code (`cmd`) primary agents and
subagents, Kilo profiles and delegated sessions, and every future runtime. An
agent profile is not authorized for execution through this repository until
its runtime has a dispatch hook or a tested repository adapter enforcing this
contract. The generic process wrapper alone is not a full harness.

The shared contract is `comfyuipod.agent-run` version 1. Studio enforces it in
`studio/comfy_studio/agent/harness.py`; the OpenCode plugin, Command Code mod,
and Kilo plugin implement runtime adapters that emit the same record shape
under `.agent-runs/`.
It records the task hash, runtime/model identity, parent run, role, allowed
tools, iteration/LLM/context/time budgets, checkpoints, tool evidence, and a
terminal status. It deliberately excludes raw prompts, credentials, tool
arguments, and tool results. Context summaries are lossy, so decisions and
evidence must be checkpointed outside model context.

## Runtime requirements

1. The runtime adapter creates the run record and supplies the contract to the
   agent before its first model request.
2. Restrict tools at dispatch time, not only in the prompt or model schema.
3. Enforce iteration, tool, context, and wall-time limits the runtime exposes.
   Check cancellation and wall time at safe boundaries; record limits the
   runtime cannot interrupt as advisory.
4. Persist checkpoints and a terminal status (`completed`, `failed`,
   `cancelled`, `max_iterations`, `budget_exhausted`,
   `wall_budget_exhausted`, or `interrupted`).
5. A child run inherits the parent's task identity, tool subset, and remaining
   shared budgets. It cannot widen them or spawn an untracked grandchild.
6. Report evidence and limitations; a clean exit alone does not prove task
   correctness.

## Runtime coverage

| Runtime | Tool enforcement | Shared budgets | Context compaction | Durable evidence |
| --- | --- | --- | --- | --- |
| Studio | Tool allowlist checked in dispatch | LLM calls, iterations, wall/cancel checks at loop boundaries | Bounds messages before model calls | Project-scoped run store |
| OpenCode | Plugin filters request tools and rejects unlisted tool dispatch; only `harnessed-builder` can be spawned | Shared LLM/tool budgets; 12-step agent cap; wall time checked at tool/model boundaries | Native automatic compaction uses the selected model's context window and 20% reserve; compaction keeps checkpoint guidance | `.agent-runs/<run-id>/run.json` and `checkpoint.md` |
| Command Code | Mod filters primary tools; native child profile limits the child tool set and four turns | Primary LLM/tool budgets and wall time enforced at loop boundaries; child turns are capped by the runtime profile, while child tool calls/tokens are observed after dispatch and cannot be interrupted by the parent mod | Primary mod compacts at 75% of its measured model window; native children use the host's configured compaction policy, but their context window is not exposed to the parent mod | Parent and each child have separate durable run records; child tool-call/token evidence is collected from native subagent lifecycle events |
| Kilo | Plugin rejects unlisted tools at dispatch; if task delegation is exposed, it targets registered project profiles and child tool sets intersect the parent's | Shared LLM/tool call and wall-time budgets enforced at model/tool boundaries; child depth is capped at three | Native auto-compaction is configured at 75% of the selected model's available context window; the plugin preserves checkpoint guidance | `.agent-runs/<run-id>/run.json` and `checkpoint.md` for each session |
| Future runtimes | Must add a runtime-specific dispatch hook before project agents may be launched | Enforce each exposed limit; record unsupported controls as limitations | Use runtime/model metadata and native compaction hooks | Same `comfyuipod.agent-run` record |

The OpenCode plugin instruments primary and child sessions. Command Code's
native children are recorded by the parent's subagent lifecycle observer and
bounded by the child's runtime profile; the parent mod cannot interrupt their
tool calls mid-run. Command Code does not auto-load a project mod, so its
sessions must start through `scripts/cmd-agent`; direct CLI sessions are not
covered. A runtime that bypasses its repository plugin/mod/adapter is an
untracked run and does not satisfy this repository's harness requirement.
Runtime hooks cannot interrupt a provider request already in flight; those
time limits are enforced at the next safe boundary.

Kilo's `.kilo/plugin/agent-harness.ts` is auto-loaded for project CLI and editor
sessions. It creates a record and injects the run contract before the first
model request, enforces tool and shared call budgets, and links sessions whose
Kilo runtime event declares a parent ID. If a task tool is exposed, child tool
allowlists are the intersection of parent and registered child profile
policies. `.kilo/opencode.jsonc` sets Kilo's
model-aware automatic compaction threshold to 75%. Profiles without a runtime
hook cannot be treated as covered merely because their prompt mentions the
harness.

Resource sizing and context thresholds must be derived from the current
host/runtime metadata, not from fixed RAM, VRAM, or model-window profiles. The
harness records limits and measurements; it does not pretend a CPU run
establishes GPU capacity or model quality.

## Local runtime references

- [OpenCode plugin hooks and lifecycle](https://opencode.ai/v2/docs/build/plugins/server/)
- [OpenCode agents and permissions](https://opencode.ai/v2/docs/agents/)
- [OpenCode compaction](https://opencode.ai/docs/compaction/)
- [Command Code mods and hooks](https://commandcode.ai/docs/mods)
- [Kilo plugins and runtime hooks](https://kilo.ai/docs/automate/extending/plugins)
