# OpenCode context compaction and quota fallback

This note records the repository policy for long agent sessions. It is based
on OpenCode's official documentation and local workstation verification on
2026-11-11. Recheck model availability and behavior after OpenCode updates.

## Context policy

- OpenCode's automatic compaction is enabled for project sessions. The
  project configuration reserves 20% of each selected model's context window,
  so compaction starts near 80% usage, and retains the most recent 12,000
  tokens beside the summary.
- The first installed summary candidate, `qwen3.5:0.8b-opencode64k`, produced
  an inaccurate test paraphrase with invented recommendations, so it is not
  accepted as a summary model.
- The selected local model is `ollama/qwen3.5:2b-opencode64k`, derived from
  the official Ollama `qwen3.5:2b-q4_K_M` tag. On this workstation,
  `ollama ps` verified the alias running at 65,536-token context, 2.2 GB
  resident size, and 100% GPU execution. A direct API summarization check with
  thinking disabled preserved all supplied facts. These numbers describe this
  machine and this prompt only.
- OpenCode documents that an unresolved compaction model falls back to the
  active session model. On machines without the local model, summaries may
  therefore use the current remote model instead.
- Compaction is lossy. Durable decisions, acceptance evidence, and pending
  steps belong in files, commits, or the task tracker before the context is
  summarized. The retained tail is not a replacement for durable state.

## OpenCode Go quota fallback

- Go quotas are model-specific and include a rolling five-hour window; the
  official Go page says free models remain available after a Go limit is
  reached, with their own daily request limits.
- Use the repository's `free-builder` agent or
  `scripts/opencode-free-builder "<task>"` after a paid model reports a quota
  limit. The agent pins `opencode-go/beam-free`, which was listed by the local
  OpenCode model catalog and worked in this environment. Model catalogs and
  eligibility can change; check `/models` if selection fails.
- OpenCode does not automatically convert a failed paid request into another
  model. This is an explicit, no-cost reroute. If the free model is unavailable
  or rate limited, stop retries and report it.

## Applicability and limitations

The 20% reserve is relative to each session model's declared context window,
not a shared token count across parent and child sessions. Each subagent has a
separate context and compacts under its own model limit. A smaller local
summary model may truncate older material when asked to summarize a much larger
parent context, so critical state must be recorded in the repo. Background
compaction is intentionally left disabled because OpenCode documents it as
experimental and because its unused summaries still incur compute.

These controls reduce remote context tokens and can run summaries on local
hardware; they do not reduce the actual configured model's context requirement
for a single oversized prompt or guarantee equivalent task quality after a
lossy summary. A direct `opencode run` with the local 2B model took more than
two minutes while loading the full CLI prompt in this environment; it was
stopped. The direct Ollama summary call completed, but end-to-end OpenCode
compaction latency and long-context summary quality still need measurement.
Use the setup script and keep the 0.8B tag only as a downloaded artifact, not
as the configured summarizer.

## Sources

- [OpenCode compaction](https://opencode.ai/docs/compaction/) — thresholds,
  retained tail, summary model, limitations, and background behavior.
- [OpenCode configuration](https://opencode.ai/docs/config/) — project and
  global configuration behavior.
- [OpenCode agents](https://opencode.ai/docs/agents/) — custom Markdown
  subagents and model pinning.
- [OpenCode Go](https://opencode.ai/console/go) — model-specific five-hour,
  weekly, monthly, and free-model allowance explanation.
- [OpenCode models](https://opencode.ai/docs/models/) — free model catalog
  behavior and model selection.
