import {createHash, randomUUID} from 'node:crypto';
import {appendFile, mkdir, rename, writeFile} from 'node:fs/promises';
import {dirname, resolve} from 'node:path';

const MOD_ID = 'comfyuipod-agent-harness';
const CONTRACT_MARKER = 'COMFYUIPOD_AGENT_RUN_CONTRACT=';
const DEFAULT_LIMITS = {turns: 12, llmCalls: 24, toolCalls: 48};
const ALLOWED_TOOLS = new Set([
	'read_file', 'read_directory', 'glob', 'grep', 'write_file', 'edit_file',
	'shell_command', 'agent', 'agent_output',
]);
const RUNS = new WeakMap();
const GROUPS = new Map();
const CHILD_RUNS = new Map();

function boundedInt(name, fallback) {
	const value = Number.parseInt(process.env[name] ?? '', 10);
	return Number.isSafeInteger(value) && value > 0 ? value : fallback;
}

function asText(value) {
	if (typeof value === 'string') return value;
	if (Array.isArray(value)) return value.map(asText).join('\n');
	if (value && typeof value === 'object') {
		if (typeof value.text === 'string') return value.text;
		if (typeof value.content === 'string') return value.content;
	}
	return '';
}

function lastUserText(messages) {
	for (let index = (messages?.length ?? 0) - 1; index >= 0; index -= 1) {
		if (messages[index]?.role === 'user') return asText(messages[index].content);
	}
	return '';
}

function readParentContract(messages) {
	const match = lastUserText(messages).match(/COMFYUIPOD_AGENT_RUN_CONTRACT=(\{[^\n]+\})/);
	if (!match) return null;
	try {
		const parsed = JSON.parse(match[1]);
		if (typeof parsed.task_id !== 'string' || typeof parsed.parent_run_id !== 'string') return null;
		return parsed;
	} catch {
		return null;
	}
}

function makeRecord(cmd, ctx, state) {
	const task = lastUserText(state?.messages).replace(/\n?COMFYUIPOD_AGENT_RUN_CONTRACT=\{[^\n]+\}/, '');
	const parent = readParentContract(state?.messages);
	const groupId = parent?.task_id ?? randomUUID();
	const group = GROUPS.get(groupId) ?? {
		remaining_llm_calls: Math.min(parent?.remaining_llm_calls ?? DEFAULT_LIMITS.llmCalls, boundedInt('COMFYUIPOD_AGENT_MAX_LLM_CALLS', DEFAULT_LIMITS.llmCalls)),
		remaining_tool_calls: Math.min(parent?.remaining_tool_calls ?? DEFAULT_LIMITS.toolCalls, boundedInt('COMFYUIPOD_AGENT_MAX_TOOL_CALLS', DEFAULT_LIMITS.toolCalls)),
		active_children: 0,
	};
	GROUPS.set(groupId, group);
	const runId = randomUUID();
	const root = resolve(ctx?.cwd || process.cwd());
	const runDir = resolve(root, '.agent-runs', runId);
	const contextUsage = (() => { try { return cmd.getContextUsage?.(); } catch { return undefined; } })();
	return {
		group,
		groupId,
		workspaceRoot: root,
			started_ms: Date.now(),
		path: resolve(runDir, 'run.json'),
		checkpointPath: resolve(runDir, 'checkpoint.md'),
		lastCheckpointCount: 0,
		record: {
			spec: {
				schema: 'comfyuipod.agent-run',
				schema_version: 1,
				run_id: runId,
				task_id: groupId,
				runtime_id: 'command-code',
				model_id: cmd.getModel?.() ?? 'runtime-reported-on-first-request',
				task_sha256: createHash('sha256').update(task).digest('hex'),
				parent_run_id: parent?.parent_run_id ?? null,
				role: parent ? 'subagent' : 'primary',
				allowed_tools: [...ALLOWED_TOOLS].sort(),
				max_iterations: Math.min(parent?.remaining_llm_calls ?? DEFAULT_LIMITS.turns, boundedInt('COMFYUIPOD_AGENT_MAX_TURNS', DEFAULT_LIMITS.turns)),
				max_llm_calls: group.remaining_llm_calls,
				max_tool_calls: group.remaining_tool_calls,
				max_context_messages: null,
				max_context_chars: null,
				max_wall_seconds: boundedInt('COMFYUIPOD_AGENT_MAX_WALL_SECONDS', 1800),
				working_directory: root,
				created_at: new Date().toISOString(),
				context_window: contextUsage?.contextWindow ?? null,
				context_percent: contextUsage?.percent ?? null,
			},
			status: 'running',
			started_at: new Date().toISOString(),
			checkpoints: [],
			tool_evidence: [],
			usage: {llm_calls: 0, tool_calls: 0, tokens: 0, compactions: []},
		},
	};
}

function makeChildRecord(parent, toolCallId, input) {
	const runId = randomUUID();
	const runDir = dirname(parent.path);
	const path = resolve(runDir, `child-${runId}`, 'run.json');
	const prompt = typeof input?.prompt === 'string' ? input.prompt : '';
	const maxTurns = 4;
	const child = {
		path,
		checkpointPath: resolve(dirname(path), 'checkpoint.md'),
		lastCheckpointCount: 0,
		toolCallId,
		group: parent.group,
		started_ms: Date.now(),
		reservedToolCalls: Math.max(0, parent.group.remaining_tool_calls),
		record: {
			spec: {
				schema: 'comfyuipod.agent-run', schema_version: 1,
				run_id: runId, task_id: parent.groupId, runtime_id: 'command-code',
				model_id: 'unexposed-by-command-code-subagent-events',
				task_sha256: createHash('sha256').update(prompt).digest('hex'),
				parent_run_id: parent.record.spec.run_id, role: 'subagent',
				allowed_tools: ['edit_file', 'grep', 'glob', 'read_directory', 'read_file', 'shell_command', 'write_file'],
				max_iterations: maxTurns, max_llm_calls: maxTurns,
				max_tool_calls: Math.max(0, parent.group.remaining_tool_calls),
				max_context_messages: null, max_context_chars: null,
				max_wall_seconds: parent.record.spec.max_wall_seconds,
				working_directory: parent.workspaceRoot,
				created_at: new Date().toISOString(), context_window: null,
				enforcement: {
					turns: 'enforced by Command Code agent profile',
					tool_allowlist: 'enforced by Command Code agent profile',
					tool_call_budget: 'observed by parent; native subagent cannot be interrupted by this mod',
					context_window: 'not exposed for nested subagent',
				},
			},
			status: 'running', started_at: new Date().toISOString(),
			checkpoints: [{at: new Date().toISOString(), stage: 'subagent_dispatched'}],
			tool_evidence: [], usage: {llm_calls: null, tool_calls: 0, tokens: 0, compactions: []},
		},
	};
	return child;
}

async function persist(run) {
	const queued = (run.persistence ?? Promise.resolve()).catch(() => {}).then(async () => {
		await mkdir(dirname(run.path), {recursive: true});
		const temporary = `${run.path}.${randomUUID()}.tmp`;
		await writeFile(temporary, `${JSON.stringify(run.record, null, 2)}\n`, {mode: 0o600});
		await rename(temporary, run.path);
		if (run.checkpointPath) {
			if (!run.checkpointInitialized) {
				await writeFile(run.checkpointPath, '# Agent run checkpoints\n', {mode: 0o600});
				run.checkpointInitialized = true;
			}
			const fresh = run.record.checkpoints.slice(run.lastCheckpointCount ?? 0);
			if (fresh.length) {
				await appendFile(run.checkpointPath, `${fresh.map(item => `- ${item.at} ${item.stage}${item.turn ? ` (turn ${item.turn})` : ''}${item.event_type ? `: ${item.event_type}` : ''}`).join('\n')}\n`);
				run.lastCheckpointCount = run.record.checkpoints.length;
			}
		}
	});
	run.persistence = queued;
	await queued;
}

function ensureRun(cmd, ctx, state) {
	if (!ctx || (typeof ctx !== 'object' && typeof ctx !== 'function')) return null;
	let run = RUNS.get(ctx);
	if (!run) {
		run = makeRecord(cmd, ctx, state);
		RUNS.set(ctx, run);
	}
	return run;
}

export default function (cmd) {
	cmd.on('subagent_start', event => {
		const child = CHILD_RUNS.get(event.toolCallId);
		if (!child) return;
		child.record.checkpoints.push({at: new Date().toISOString(), stage: 'subagent_started', subagent_type: event.subagentType ?? null});
		void persist(child);
	});
	cmd.on('subagent_progress', event => {
		const child = CHILD_RUNS.get(event.toolCallId);
		if (!child) return;
		const withinBudget = child.group.remaining_tool_calls > 0;
		child.group.remaining_tool_calls = Math.max(0, child.group.remaining_tool_calls - 1);
		child.record.usage.tool_calls += 1;
		child.record.usage.tokens = Math.max(child.record.usage.tokens, Number(event.tokensUsed ?? 0));
		child.record.tool_evidence.push({at: new Date().toISOString(), name: event.toolName ?? 'unknown', input_sha256: createHash('sha256').update(JSON.stringify(event.toolInput ?? {})).digest('hex'), execution: 'observed_by_parent', within_shared_budget: withinBudget});
		if (!withinBudget) child.record.status = 'budget_exhausted';
		child.record.tool_evidence = child.record.tool_evidence.slice(-200);
		child.record.checkpoints.push({at: new Date().toISOString(), stage: 'subagent_tool_observed'});
		child.record.checkpoints = child.record.checkpoints.slice(-100);
		void persist(child);
	});
	cmd.on('subagent_stop', event => {
		const child = CHILD_RUNS.get(event.toolCallId);
		if (!child) return;
		child.record.usage.tokens = Math.max(child.record.usage.tokens, Number(event.tokensUsed ?? 0));
		const elapsed = (Date.now() - child.started_ms) / 1000;
		if (child.record.status === 'running') child.record.status = elapsed > child.record.spec.max_wall_seconds ? 'wall_budget_exhausted' : 'completed';
		child.record.finished_at = new Date().toISOString();
		child.record.completion_evidence = {subagent_stop_observed: true, tokens_used: child.record.usage.tokens};
		child.record.checkpoints.push({at: child.record.finished_at, stage: 'subagent_stopped'});
		void persist(child);
	});
	cmd.on('mod_error', async event => {
		const root = resolve(process.cwd());
		const path = resolve(root, '.agent-runs', 'command-code-mod-errors.jsonl');
		await mkdir(dirname(path), {recursive: true});
		const error = event.error?.message ?? String(event.error ?? '');
		await appendFile(path, `${JSON.stringify({at: new Date().toISOString(), mod_id: event.modId ?? null, hook: event.hook ?? null, error_name: event.error?.name ?? null, error: error.slice(0, 500)})}\n`);
	});
	cmd.hooks({
		appendSystemPrompt: async ({state}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return 'ComfyUIPod agent harness is active. Stop if the run record cannot be created.';
			await persist(run);
			return [
				'ComfyUIPod universal agent harness is active for this run.',
				`Run ID: ${run.record.spec.run_id}. Task ID: ${run.groupId}.`,
				`Durable checkpoint file: ${run.path.replace(/run\.json$/, 'checkpoint.md')}.`,
				'Use only the tools declared by this run contract; keep work within the remaining budgets.',
				run.context_compaction_failed ? 'Context compaction failed; do not continue tool work. Return a concise status now.' : '',
				'Persist decisions, evidence, and next steps in the checkpoint file before context compaction.',
				'Child agents must use the harnessed-builder profile.',
			].join(' ');
		},

		onTurnStart: async ({state, turnNumber}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return state;
			run.record.usage.llm_calls += 1;
			run.group.remaining_llm_calls = Math.max(0, run.group.remaining_llm_calls - 1);
			try {
				const activeTools = cmd.getActiveTools?.() ?? [];
				cmd.setActiveTools?.(activeTools.filter(name => ALLOWED_TOOLS.has(name)));
			} catch { /* the pre-tool hook below remains the enforcement boundary */ }
			const usage = (() => { try { return cmd.getContextUsage?.(); } catch { return undefined; } })();
			if (usage) {
				run.record.spec.context_window = usage.contextWindow ?? null;
				run.record.spec.context_percent = usage.percent ?? null;
				if (Number.isFinite(usage.percent) && usage.percent >= 75) {
					try {
						if (!cmd.sessions?.compact) throw new Error('runtime compaction API unavailable');
						await cmd.sessions.compact();
						run.record.usage.compactions.push({at: new Date().toISOString(), reason: 'dynamic_context_threshold', threshold_percent: 75, observed_percent: usage.percent, context_window: usage.contextWindow ?? null, status: 'completed'});
					} catch (error) {
						run.context_compaction_failed = true;
						run.record.usage.compactions.push({at: new Date().toISOString(), reason: 'dynamic_context_threshold', threshold_percent: 75, observed_percent: usage.percent, context_window: usage.contextWindow ?? null, status: 'failed', error_name: error?.name ?? 'Error'});
						try { cmd.setActiveTools?.([]); } catch { /* no tools is safer if compaction failed */ }
					}
				}
			}
			run.record.checkpoints.push({at: new Date().toISOString(), stage: 'turn_started', turn: turnNumber});
			run.record.checkpoints = run.record.checkpoints.slice(-100);
			await persist(run);
			return state;
		},

		beforeToolCall: async ({toolCallId, toolName, input, state}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return {block: true, additionalContext: 'Harness failed to create this run; tool call denied.'};
			if (!ALLOWED_TOOLS.has(toolName)) {
				return {block: true, additionalContext: `Harness denied tool ${toolName}: it is outside this agent profile allowlist.`};
			}
			run.record.usage.tool_calls += 1;
			if (run.group.remaining_tool_calls <= 0 || run.record.usage.tool_calls > boundedInt('COMFYUIPOD_AGENT_MAX_TOOL_CALLS', DEFAULT_LIMITS.toolCalls)) {
				return {block: true, additionalContext: 'Harness tool-call budget exhausted.'};
			}
			run.group.remaining_tool_calls -= 1;
			if (toolName === 'agent') {
				const type = input?.subagent_type ?? input?.agent_type ?? input?.agent;
				if (type !== 'harnessed-builder') {
					return {block: true, additionalContext: 'Harness permits child runs only through the harnessed-builder profile.'};
				}
				const childTurnBudget = 4;
				if (run.group.remaining_llm_calls < childTurnBudget || run.group.remaining_tool_calls < 1) {
					return {block: true, additionalContext: 'Harness parent-child shared budget is exhausted.'};
				}
				run.group.remaining_llm_calls -= childTurnBudget;
				const prompt = typeof input.prompt === 'string' ? input.prompt : '';
				const child = makeChildRecord(run, toolCallId, input);
				CHILD_RUNS.set(toolCallId, child);
				await persist(child);
				const contract = {
					task_id: run.groupId,
					parent_run_id: run.record.spec.run_id,
					child_run_id: child.record.spec.run_id,
					workspace_root: run.workspaceRoot,
					checkpoint_path: child.path.replace(/run\.json$/, 'checkpoint.md'),
					max_iterations: child.record.spec.max_iterations,
					max_tool_calls: child.record.spec.max_tool_calls,
					remaining_tool_calls: run.group.remaining_tool_calls,
				};
				return {input: {...input, prompt: `${prompt}\n\n${CONTRACT_MARKER}${JSON.stringify(contract)}`}};
			}
			return undefined;
		},

		afterToolCall: async ({toolCallId, toolName, isError, result, state}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return undefined;
			run.record.tool_evidence.push({at: new Date().toISOString(), tool_call_id: toolCallId, name: toolName, allowed: ALLOWED_TOOLS.has(toolName), executed: true, ok: !isError});
			run.record.tool_evidence = run.record.tool_evidence.slice(-200);
			if (toolName === 'agent') {
				const child = CHILD_RUNS.get(toolCallId);
				if (child) {
					const resultText = asText(result);
					if (isError) child.record.status = 'failed';
					else if (child.record.status === 'running') child.record.status = 'completed';
					child.record.finished_at = new Date().toISOString();
					child.record.completion_evidence = {tool_error: Boolean(isError), result_sha256: createHash('sha256').update(resultText).digest('hex')};
					if (isError) child.record.error_code = 'subagent_tool_failed';
					await persist(child);
					CHILD_RUNS.delete(toolCallId);
				}
			}
			await persist(run);
			return undefined;
		},

		onTurnEnd: async ({state, turnNumber, usage}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return state;
			const tokens = Number(usage?.inputTokens ?? 0) + Number(usage?.outputTokens ?? 0);
			run.record.usage.tokens += Number.isFinite(tokens) ? tokens : 0;
			run.record.checkpoints.push({at: new Date().toISOString(), stage: 'turn_completed', turn: turnNumber, usage: usage ?? null});
			run.record.checkpoints = run.record.checkpoints.slice(-100);
			await persist(run);
			return state;
		},

		shouldStopAfterTurn: ({turnNumber}, ctx) => {
			const run = ctx && RUNS.get(ctx);
			return Boolean(run && (
				turnNumber >= run.record.spec.max_iterations
				|| Date.now() - run.started_ms >= run.record.spec.max_wall_seconds * 1000
				|| run.group.remaining_llm_calls <= 0
				|| run.group.remaining_tool_calls <= 0
			));
		},

		onRunEnd: async ({state, result}, ctx) => {
			const run = ensureRun(cmd, ctx, state);
			if (!run) return;
			for (const [toolCallId, child] of CHILD_RUNS) {
				if (child.record.spec.parent_run_id !== run.record.spec.run_id) continue;
				child.record.status = 'interrupted';
				child.record.finished_at = new Date().toISOString();
				child.record.error_code = 'parent_run_ended_before_subagent_terminal_event';
				child.record.checkpoints.push({at: child.record.finished_at, stage: 'parent_run_ended'});
				await persist(child);
				CHILD_RUNS.delete(toolCallId);
			}
			run.record.status = result?.stopReason === 'interrupted' ? 'interrupted' : result?.stopReason === 'max_turns' ? 'max_iterations' : Date.now() - run.started_ms >= run.record.spec.max_wall_seconds * 1000 ? 'wall_budget_exhausted' : run.group.remaining_llm_calls <= 0 || run.group.remaining_tool_calls <= 0 ? 'budget_exhausted' : 'completed';
			run.record.finished_at = new Date().toISOString();
			run.record.completion_evidence = {stop_reason: result?.stopReason ?? null, turns: result?.turnCount ?? null, final_text_sha256: createHash('sha256').update(String(result?.finalText ?? '')).digest('hex')};
			await persist(run);
		},
	});
}
