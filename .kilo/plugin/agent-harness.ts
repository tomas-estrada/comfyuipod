import { createHash, randomUUID } from "node:crypto"
import { mkdirSync, renameSync, writeFileSync } from "node:fs"
import { mkdir, rename, writeFile } from "node:fs/promises"
import { dirname, resolve } from "node:path"

// Kilo's project plugin is loaded for CLI and editor sessions. It records every
// primary and delegated session and enforces the shared tool, call, and time
// budgets at the runtime's dispatch boundaries.
const SCHEMA = "comfyuipod.agent-run"
const DEFAULTS = { llmCalls: 24, toolCalls: 48, wallSeconds: 1800 }
const RECORDED_EVENTS = new Set(["session.created", "session.idle", "session.error", "session.deleted", "session.status", "session.compacted"])
const DEFAULT_TOOLS = new Set(["read", "glob", "grep", "list", "bash", "edit", "write", "apply_patch", "webfetch", "websearch", "codesearch", "lsp", "skill", "task", "todowrite", "todoread", "plan_exit"])
const PROFILE_TOOLS: Record<string, Set<string>> = {
	implementer: new Set(["read", "glob", "grep", "list", "bash", "edit", "write", "apply_patch", "webfetch", "websearch", "codesearch", "lsp", "skill", "task", "todowrite", "todoread", "plan_exit"]),
	reviewer: new Set(["read", "glob", "grep", "list", "bash", "webfetch", "websearch", "codesearch", "lsp", "skill", "task", "todoread"]),
	"scrutiny-auditor": new Set(["read", "glob", "grep", "list", "bash", "webfetch", "websearch", "codesearch", "lsp", "skill", "task", "todoread"]),
	"harnessed-builder": new Set(["read", "glob", "grep", "list", "bash", "edit", "write", "apply_patch", "webfetch", "websearch", "codesearch", "lsp", "skill", "todowrite", "todoread"]),
}
const RUNS = new Map<string, any>()
const PARENTS = new Map<string, string>()
const GROUPS = new Map<string, any>()
let EXIT_HANDLER_INSTALLED = false

function positiveEnv(name: string, fallback: number): number {
	const parsed = Number.parseInt(process.env[name] ?? "", 10)
	return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback
}

function sessionInfo(event: any): any {
	const value = event?.properties ?? event?.data ?? event
	return value?.info ?? value?.session ?? value
}

function safeModelId(model: any): string {
	return `${model?.providerID ?? "unknown"}/${model?.modelID ?? model?.id ?? "unknown"}`
}

async function persist(run: any): Promise<void> {
	await mkdir(dirname(run.path), { recursive: true })
	const temporary = `${run.path}.${randomUUID()}.tmp`
	await writeFile(temporary, `${JSON.stringify(run.record, null, 2)}\n`, { mode: 0o600 })
	await rename(temporary, run.path)
}

function installExitHandler(): void {
	if (EXIT_HANDLER_INSTALLED) return
	EXIT_HANDLER_INSTALLED = true
	process.once("exit", code => {
		for (const run of RUNS.values()) {
			if (run.record.status !== "running") continue
			run.record.status = "interrupted"
			run.record.finished_at = new Date().toISOString()
			run.record.checkpoints.push({ at: run.record.finished_at, stage: "process_exit", exit_code: code })
			run.record.checkpoints = run.record.checkpoints.slice(-100)
			try {
				mkdirSync(dirname(run.path), { recursive: true, mode: 0o700 })
				const temporary = `${run.path}.${randomUUID()}.tmp`
				writeFileSync(temporary, `${JSON.stringify(run.record, null, 2)}\n`, { mode: 0o600 })
				renameSync(temporary, run.path)
			} catch {
				// Exit handlers must not mask the runtime's original exit status.
			}
		}
	})
}

function groupFor(taskId: string, parent?: any): any {
	let group = GROUPS.get(taskId)
	if (!group) {
		group = parent?.group ?? {
			remaining_llm_calls: positiveEnv("COMFYUIPOD_AGENT_MAX_LLM_CALLS", DEFAULTS.llmCalls),
			remaining_tool_calls: positiveEnv("COMFYUIPOD_AGENT_MAX_TOOL_CALLS", DEFAULTS.toolCalls),
			started_ms: Date.now(),
			depth: 0,
		}
		GROUPS.set(taskId, group)
	}
	return group
}

async function createRun(directory: string, sessionID: string, agent: string, model: any, taskText: string): Promise<any> {
	const existing = RUNS.get(sessionID)
	if (existing) return existing
	const parentSessionID = PARENTS.get(sessionID)
	const parent = parentSessionID ? RUNS.get(parentSessionID) : undefined
	const runId = randomUUID()
	const taskId = parent?.record.spec.task_id ?? randomUUID()
	const group = groupFor(taskId, parent)
	const depth = parent ? parent.depth + 1 : 0
	const root = resolve(directory || process.cwd())
	const runDir = resolve(root, ".agent-runs", runId)
	const modelContext = [model?.limit?.context, model?.contextWindow, model?.context_window].find(Number.isSafeInteger) ?? null
	const role = agent || "primary"
	const profileTools = PROFILE_TOOLS[role] ?? DEFAULT_TOOLS
	const inheritedTools = parent ? new Set<string>(parent.record.spec.allowed_tools) : null
	const allowedTools = [...profileTools].filter(tool => !inheritedTools || inheritedTools.has(tool)).sort()
	const taskHash = createHash("sha256").update(taskText || sessionID).digest("hex")
	const record = {
		spec: {
			schema: SCHEMA,
			schema_version: 1,
			run_id: runId,
			task_id: taskId,
			runtime_id: "kilo",
			model_id: safeModelId(model),
			task_sha256: taskHash,
			parent_run_id: parent?.record.spec.run_id ?? null,
			role,
			allowed_tools: allowedTools,
			max_iterations: positiveEnv("COMFYUIPOD_AGENT_MAX_LLM_CALLS", DEFAULTS.llmCalls),
			max_llm_calls: group.remaining_llm_calls,
			max_tool_calls: group.remaining_tool_calls,
			max_context_messages: null,
			max_context_chars: null,
			max_wall_seconds: positiveEnv("COMFYUIPOD_AGENT_MAX_WALL_SECONDS", DEFAULTS.wallSeconds),
			working_directory: root,
			context_window: modelContext,
			compaction_threshold_percent: 75,
			created_at: new Date().toISOString(),
		},
		status: "running",
		started_at: new Date().toISOString(),
		checkpoints: [],
		tool_evidence: [],
		usage: { llm_calls: 0, tool_calls: 0, compactions: [] as any[] },
	}
	const run = { sessionID, path: resolve(runDir, "run.json"), checkpointPath: resolve(runDir, "checkpoint.md"), record, group, started_ms: Date.now(), depth }
	RUNS.set(sessionID, run)
	await persist(run)
	await writeFile(run.checkpointPath, `# Agent checkpoint\n\n- Run: ${runId}\n- Task: ${taskId}\n- Status: started\n- Task SHA-256: ${taskHash}\n\n## Decisions\n\n## Verified evidence\n\n## Blockers\n\n## Next action\n`, { mode: 0o600 })
	return run
}

function messageText(value: any): string {
	return JSON.stringify(value ?? "")
}

function contractText(run: any): string {
	return [
		`Repository agent harness contract (${SCHEMA} v1). Run ${run.record.spec.run_id}; task ${run.record.spec.task_id}.`,
		`Allowed tools: ${run.record.spec.allowed_tools.join(", ") || "none"}. Calls left in shared task budget: LLM ${run.group.remaining_llm_calls}, tools ${run.group.remaining_tool_calls}.`,
		`Checkpoint decisions, evidence, and next steps at ${run.path.replace(/run\.json$/, "checkpoint.md")} before compaction. Do not include prompts, credentials, or raw tool results in run records.`,
		"Do not start an untracked agent. Delegate only through Kilo's task tool; child sessions receive their own run record and share this task budget.",
	].join(" ")
}

const AgentHarness: any = async ({ directory }: any) => {
	installExitHandler()
	return {
	"chat.message": async (input: any, output: any) => {
		const parts = output?.parts ?? []
		const run = await createRun(directory, input.sessionID, input.agent ?? "primary", input.model, messageText(parts))
		run.record.checkpoints.push({ at: new Date().toISOString(), stage: "user_message", agent: input.agent ?? "primary" })
		await persist(run)
	},
	"chat.params": async (input: any) => {
		const run = RUNS.get(input.sessionID)
		if (!run) throw new Error("Kilo harness has no run contract; model request denied.")
		if (run.record.usage.llm_calls >= run.record.spec.max_iterations || run.group.remaining_llm_calls <= 0 || Date.now() - run.group.started_ms >= run.record.spec.max_wall_seconds * 1000) {
			run.record.status = run.group.remaining_llm_calls <= 0 || run.record.usage.llm_calls >= run.record.spec.max_iterations ? "budget_exhausted" : "wall_budget_exhausted"
			run.record.finished_at = new Date().toISOString()
			await persist(run)
			throw new Error("Kilo harness shared call or wall-time budget exhausted.")
		}
		run.group.remaining_llm_calls -= 1
		run.record.usage.llm_calls += 1
		run.record.spec.model_id = safeModelId(input.model)
		run.record.spec.context_window = [input.model?.limit?.context, input.model?.contextWindow, input.model?.context_window].find(Number.isSafeInteger) ?? run.record.spec.context_window
		run.record.checkpoints.push({ at: new Date().toISOString(), stage: "model_request", remaining_llm_calls: run.group.remaining_llm_calls, context_window: run.record.spec.context_window })
		await persist(run)
	},
	"experimental.chat.system.transform": async (input: any, output: any) => {
		const run = input.sessionID ? RUNS.get(input.sessionID) : undefined
		if (!run) throw new Error("Kilo harness has no run contract; system prompt denied.")
		output.system.push(contractText(run))
	},
	"tool.execute.before": async (input: any, output: any) => {
		const run = RUNS.get(input.sessionID)
		if (!run) throw new Error("Kilo harness has no durable run; tool call denied.")
		const allowed = new Set(run.record.spec.allowed_tools)
		if (!allowed.has(input.tool)) throw new Error(`Kilo harness denied unlisted tool: ${input.tool}`)
		if (run.group.remaining_tool_calls <= 0 || Date.now() - run.group.started_ms >= run.record.spec.max_wall_seconds * 1000) {
			throw new Error("Kilo harness shared tool or wall-time budget exhausted.")
		}
		if (input.tool === "task") {
			const target = output.args?.subagent_type
			if (!PROFILE_TOOLS[target] || run.depth >= 3) throw new Error("Kilo harness denied unregistered or over-depth child agent.")
		}
		run.group.remaining_tool_calls -= 1
		run.record.usage.tool_calls += 1
		run.record.checkpoints.push({ at: new Date().toISOString(), stage: "tool_dispatch", tool: input.tool, call_id: input.callID, remaining_tool_calls: run.group.remaining_tool_calls })
		await persist(run)
	},
	"tool.execute.after": async (input: any, output: any) => {
		const run = RUNS.get(input.sessionID)
		if (!run) return
		run.record.tool_evidence.push({ at: new Date().toISOString(), name: input.tool, call_id: input.callID, outcome: output.output ? "returned" : "empty" })
		run.record.tool_evidence = run.record.tool_evidence.slice(-200)
		await persist(run)
	},
	"experimental.session.compacting": async (input: any, output: any) => {
		const run = RUNS.get(input.sessionID)
		if (!run) throw new Error("Kilo compaction requested without an agent run contract.")
		output.context.push(`Persisted ComfyUIPod harness run: ${run.path}. Preserve current status, decisions, verified evidence, unresolved blockers, and the next action. Read checkpoint.md before continuing.`)
		run.record.usage.compactions.push({ at: new Date().toISOString(), policy: "host-model-context-window" })
		await persist(run)
	},
	event: async ({ event }: any) => {
		const info = sessionInfo(event)
		const sessionID = info?.id ?? info?.sessionID ?? info?.sessionId
		if (!sessionID) return
		if (event.type === "session.created" && info.parentID) PARENTS.set(sessionID, info.parentID)
		const run = RUNS.get(sessionID)
		if (!run || !RECORDED_EVENTS.has(event.type)) return
		if (event.type === "session.idle" && run.record.status === "running") run.record.status = "completed"
		else if (event.type === "session.error" && run.record.status === "running") run.record.status = "failed"
		else if (event.type === "session.deleted" && run.record.status === "running") run.record.status = "interrupted"
		else if (event.type === "session.status" && (info?.status?.type ?? info?.status) === "idle" && run.record.status === "running") run.record.status = "interrupted"
		if (["session.idle", "session.error", "session.deleted"].includes(event.type) && run.record.status !== "running") run.record.finished_at ??= new Date().toISOString()
		if (event.type === "session.status" && (info?.status?.type ?? info?.status) === "idle" && run.record.status !== "running") run.record.finished_at ??= new Date().toISOString()
		run.record.checkpoints.push({ at: new Date().toISOString(), stage: "runtime_event", event_type: event.type })
		run.record.checkpoints = run.record.checkpoints.slice(-100)
		await persist(run)
	}
}
}

export default { id: "comfyuipod-agent-harness", server: AgentHarness }
