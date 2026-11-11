import {createHash, randomUUID} from 'node:crypto';
import {mkdirSync, renameSync, writeFileSync} from 'node:fs';
import {dirname, resolve} from 'node:path';

const ROOT_ID = 'comfyuipod.agent-harness';
const CHILD_MARKER = 'COMFYUIPOD_AGENT_RUN_CONTRACT=';
const TOOL_ALLOWLIST = new Set(['read', 'glob', 'grep', 'shell', 'write', 'edit', 'webfetch', 'websearch', 'subagent']);
const CHILD_AGENT = 'harnessed-builder';
const RUNS = new Map();
const GROUPS = new Map();

function positiveInteger(name, fallback) {
	const parsed = Number.parseInt(process.env[name] ?? '', 10);
	return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : fallback;
}

function parentContract(messages) {
	const collect = value => typeof value === 'string' ? [value] : Array.isArray(value) ? value.flatMap(collect) : value && typeof value === 'object' ? Object.values(value).flatMap(collect) : [];
	const text = collect(messages ?? []).join('\n');
	const match = text.match(/COMFYUIPOD_AGENT_RUN_CONTRACT=(\{[^\n]+\})/);
	if (!match) return null;
	try {
		const value = JSON.parse(match[1]);
		return typeof value.task_id === 'string' && typeof value.parent_run_id === 'string' ? value : null;
	} catch {
		return null;
	}
}

function writeRecord(run) {
	mkdirSync(dirname(run.path), {recursive: true});
	const temporary = `${run.path}.tmp`;
	writeFileSync(temporary, `${JSON.stringify(run.record, null, 2)}\n`, {mode: 0o600});
	renameSync(temporary, run.path);
}

function createRun(ctx, event) {
	const parent = parentContract(event.messages);
	const taskId = parent?.task_id ?? randomUUID();
	const group = GROUPS.get(taskId) ?? {
		remaining_llm_calls: Math.min(parent?.remaining_llm_calls ?? positiveInteger('COMFYUIPOD_AGENT_MAX_LLM_CALLS', 24), positiveInteger('COMFYUIPOD_AGENT_MAX_LLM_CALLS', 24)),
		remaining_tool_calls: Math.min(parent?.remaining_tool_calls ?? positiveInteger('COMFYUIPOD_AGENT_MAX_TOOL_CALLS', 48), positiveInteger('COMFYUIPOD_AGENT_MAX_TOOL_CALLS', 48)),
	};
	GROUPS.set(taskId, group);
	const runId = randomUUID();
	const root = resolve(ctx.location?.directory || ctx.location?.project?.canonical || process.cwd());
	const runPath = resolve(root, '.agent-runs', runId, 'run.json');
	const promptHash = createHash('sha256').update(JSON.stringify(event.messages ?? [])).digest('hex');
	return {
		group,
		started_ms: Date.now(),
		path: runPath,
		record: {
			spec: {
				schema: 'comfyuipod.agent-run',
				schema_version: 1,
				run_id: runId,
				task_id: taskId,
				runtime_id: 'opencode',
				model_id: `${event.model?.providerID ?? 'unknown'}/${event.model?.id ?? 'unknown'}`,
				task_sha256: promptHash,
				parent_run_id: parent?.parent_run_id ?? null,
				role: parent ? 'subagent' : event.agent ?? 'primary',
				allowed_tools: [...TOOL_ALLOWLIST].filter(name => name !== 'subagent' || event.agent === 'build' || event.agent === 'free-builder').sort(),
				max_iterations: Math.min(parent?.remaining_llm_calls ?? positiveInteger('COMFYUIPOD_AGENT_MAX_TURNS', 12), positiveInteger('COMFYUIPOD_AGENT_MAX_TURNS', 12)),
				max_llm_calls: group.remaining_llm_calls,
				max_context_messages: null,
				max_context_chars: null,
				max_wall_seconds: positiveInteger('COMFYUIPOD_AGENT_MAX_WALL_SECONDS', 1800),
				context_window: event.model?.limit?.context ?? null,
				created_at: new Date().toISOString(),
			},
			status: 'running',
			started_at: new Date().toISOString(),
			checkpoints: [],
			tool_evidence: [],
			usage: {llm_calls: 0, tool_calls: 0, compactions: []},
		},
	};
}

function getRun(ctx, sessionId, event) {
	let run = RUNS.get(sessionId);
	if (!run) {
		run = createRun(ctx, event);
		RUNS.set(sessionId, run);
		writeRecord(run);
	}
	return run;
}

export default {
	id: ROOT_ID,
	async setup(ctx) {
		await ctx.session.hook('context', async event => {
			const run = getRun(ctx, event.sessionID, event);
			run.record.usage.llm_calls += 1;
			run.group.remaining_llm_calls = Math.max(0, run.group.remaining_llm_calls - 1);
			try {
				const catalog = await ctx.model.list();
				const models = Array.isArray(catalog) ? catalog : catalog?.data ?? [];
				const model = models.find(item => item.providerID === event.model?.providerID && item.id === event.model?.id);
				if (model?.limit?.context) run.record.spec.context_window = model.limit.context;
			} catch { /* keep the runtime-supplied context-window value when the catalog is unavailable */ }
			const allowed = new Set(TOOL_ALLOWLIST);
			if (event.agent !== 'build' && event.agent !== 'free-builder') allowed.delete('subagent');
			if (run.record.usage.llm_calls >= run.record.spec.max_iterations) {
				for (const name of Object.keys(event.tools)) delete event.tools[name];
				event.system.push({type: 'text', text: 'ComfyUIPod per-agent turn limit reached. Return the current verified status and stop.'});
			} else if (run.group.remaining_llm_calls <= 0 || Date.now() - run.started_ms >= run.record.spec.max_wall_seconds * 1000) {
				for (const name of Object.keys(event.tools)) delete event.tools[name];
				event.system.push({type: 'text', text: 'ComfyUIPod harness budget is exhausted. Return the current verified status and stop.'});
			} else {
				for (const name of Object.keys(event.tools)) if (!allowed.has(name)) delete event.tools[name];
				event.system.push({type: 'text', text: [
					`ComfyUIPod agent run ${run.record.spec.run_id}; task ${run.record.spec.task_id}.`,
					`Checkpoint decisions, evidence, and next steps at ${run.path.replace(/run\.json$/, 'checkpoint.md')} before compaction.`,
					`Use only the run tool allowlist: ${[...allowed].join(', ')}.`,
					'Automatic context compaction is configured against this model’s dynamic context window with a 20% reserve; preserve durable checkpoint details.',
				].join(' ')});
			}
				run.record.checkpoints.push({at: new Date().toISOString(), stage: 'model_request', agent: event.agent, remaining_llm_calls: run.group.remaining_llm_calls, context_window: run.record.spec.context_window ?? null});
			run.record.checkpoints = run.record.checkpoints.slice(-100);
			writeRecord(run);
		});

		await ctx.tool.hook('execute.before', event => {
			const run = RUNS.get(event.sessionID);
			if (!run) throw new Error('ComfyUIPod harness has no run record; tool call denied.');
			if (!TOOL_ALLOWLIST.has(event.tool)) throw new Error(`ComfyUIPod harness denied unlisted tool: ${event.tool}`);
			if (run.record.usage.llm_calls >= run.record.spec.max_iterations) throw new Error('ComfyUIPod harness per-agent turn limit reached.');
			if (run.group.remaining_tool_calls <= 0) throw new Error('ComfyUIPod harness tool-call budget exhausted.');
			if (Date.now() - run.started_ms >= run.record.spec.max_wall_seconds * 1000) throw new Error('ComfyUIPod harness wall-time budget exhausted.');
			run.group.remaining_tool_calls -= 1;
			run.record.usage.tool_calls += 1;
			if (event.tool === 'subagent') {
				const input = event.input ?? {};
				const agent = input.agent ?? input.agentID ?? input.subagent_type;
				if (agent !== CHILD_AGENT) throw new Error(`ComfyUIPod harness allows only the ${CHILD_AGENT} child profile.`);
				const contract = {
					task_id: run.record.spec.task_id,
					parent_run_id: run.record.spec.run_id,
					remaining_llm_calls: run.group.remaining_llm_calls,
					remaining_tool_calls: run.group.remaining_tool_calls,
				};
				input.prompt = `${input.prompt ?? ''}\n\n${CHILD_MARKER}${JSON.stringify(contract)}`;
				event.input = input;
			}
			writeRecord(run);
		});

		await ctx.tool.hook('execute.after', event => {
			const run = RUNS.get(event.sessionID);
			if (!run) return;
			run.record.tool_evidence.push({at: new Date().toISOString(), name: event.tool, outcome: event.status, allowed: TOOL_ALLOWLIST.has(event.tool)});
			run.record.tool_evidence = run.record.tool_evidence.slice(-200);
			writeRecord(run);
		});

		await ctx.session.hook('compaction', event => {
			event.system.push({type: 'text', text: 'Preserve decisions, verified evidence with paths/commands, unresolved blockers, and the next concrete action. Do not replace exact blockers with a generic summary.'});
		});

		const controller = new AbortController();
		void (async () => {
			for await (const event of ctx.event.subscribe({signal: controller.signal})) {
				const current = event.payload ?? event;
				const properties = current.properties ?? current.data ?? {};
				const sessionId = properties.sessionID ?? properties.info?.id;
				const run = sessionId ? RUNS.get(sessionId) : undefined;
				if (!run) continue;
				if (current.type === 'session.compacted' || current.type === 'session.execution.compacted') {
					run.record.usage.compactions.push({at: new Date().toISOString(), policy: 'model-dynamic-window'});
				} else if (current.type === 'session.execution.succeeded' || current.type === 'session.idle' || current.type === 'session.status' && properties.status?.type === 'idle') {
					run.record.status = run.group.remaining_llm_calls <= 0 || run.group.remaining_tool_calls <= 0 ? 'budget_exhausted' : Date.now() - run.started_ms >= run.record.spec.max_wall_seconds * 1000 ? 'wall_budget_exhausted' : 'completed';
					run.record.finished_at = new Date().toISOString();
				} else if (current.type === 'session.execution.failed' || current.type === 'session.error') {
					run.record.status = 'failed';
					run.record.finished_at = new Date().toISOString();
					run.record.error_code = current.type;
				} else if (current.type === 'session.execution.aborted' || current.type === 'session.interrupted') {
					run.record.status = 'interrupted';
					run.record.finished_at = new Date().toISOString();
				}
				if (typeof current.type === 'string' && (current.type.startsWith('session.execution.') || current.type === 'session.compacted' || current.type === 'session.idle' || current.type === 'session.status' || current.type === 'session.error')) {
					run.record.checkpoints.push({at: new Date().toISOString(), stage: 'runtime_event', event_type: current.type ?? 'unknown'});
				}
				run.record.checkpoints = run.record.checkpoints.slice(-100);
				writeRecord(run);
			}
		})().catch(error => console.error(`[${ROOT_ID}] event stream failed`, error));
		return () => controller.abort();
	},
};
