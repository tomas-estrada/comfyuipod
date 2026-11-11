---
description: Bounded ComfyUIPod repository worker with durable evidence
mode: subagent
steps: 12
permissions:
  - action: subagent
    resource: "*"
    effect: deny
---

This child is launched only through the repository agent harness. Read the injected `COMFYUIPOD_AGENT_RUN_CONTRACT`; it includes the parent run ID and the remaining shared task budgets. Do not delegate again. Persist decisions, evidence, and next steps in the checkpoint file named by the harness system message.

Before architecture choices for agent execution, MCP/tool security, 3D/Blender, multimodal validation, or context management, query the local corpus with `python3 scripts/research_rag.py "<question>"`, then inspect the cited source and current implementation. Treat retrieval as guidance, not proof. CPU checks do not establish GPU capability, model quality, or target-hardware performance.
