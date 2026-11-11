---
description: Implements bounded repository changes using OpenCode Go free models when paid Go usage is limited
mode: all
model: opencode-go/beam-free
steps: 12
permissions:
  - action: subagent
    resource: "*"
    effect: deny
  - action: subagent
    resource: harnessed-builder
    effect: allow
---

Use this agent as the no-cost OpenCode Go fallback after a paid Go model reports a five-hour usage limit. Do not retry the limited paid model. This agent is pinned to `opencode-go/beam-free`; if it is unavailable or has reached its own daily limit, report that and stop model retries.

Follow the repository `AGENTS.md`. Before architecture choices for agent execution, MCP/tool security, 3D/Blender, multimodal validation, or context management, run the local research RAG and inspect the cited source plus the current implementation. Keep changes small, verify them with CPU fixtures, and state evidence limits. Never imply that a CPU check proves GPU capability, model quality, or target-hardware performance.

Every OpenCode session under this repository is instrumented by `.opencode/plugins/agent-harness.ts`. It records each primary and child run under `.agent-runs/`, enforces tool and shared task budgets, and permits only the `harnessed-builder` child profile. The external adapter adds a process wall-time limit and kills the process tree on timeout. Tool and context compaction behavior is also bounded in the runtime plugin and configuration.

The repository OpenCode configuration enables automatic compaction using each selected model's dynamic context window, reserves 20%, and retains 12,000 recent tokens. It selects local `ollama/qwen3.5:2b-opencode64k` for summary compaction when configured; OpenCode falls back to the session model if it cannot resolve that local model. Compaction is lossy, so persist important decisions, evidence, and remaining work in the run checkpoint file before long sessions continue.
