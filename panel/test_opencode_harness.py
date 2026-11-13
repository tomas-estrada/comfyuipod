"""OpenCode repository harness behavior tests (run with Node 22 in CI)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_exhausted_turn_budget_omits_tools_so_model_can_return_summary() -> None:
    """An empty tools object is rejected by OpenCode providers; omit it entirely."""
    repository_root = Path(__file__).resolve().parents[1]
    script = r"""
import assert from 'node:assert/strict';
import {mkdtempSync, readFileSync, rmSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const root = mkdtempSync(path.join(os.tmpdir(), 'opencode-harness-test-'));
process.env.COMFYUIPOD_AGENT_MAX_TURNS = '1';
process.env.COMFYUIPOD_AGENT_MAX_LLM_CALLS = '4';
process.env.COMFYUIPOD_AGENT_MAX_TOOL_CALLS = '4';
process.env.COMFYUIPOD_AGENT_MAX_WALL_SECONDS = '30';
const {default: plugin} = await import(pathToFileURL(path.join(
  process.cwd(), '.opencode/plugins/agent-harness.ts'
)));
const hooks = new Map();
await plugin.setup({
  location: {directory: root},
  session: {hook: (name, callback) => hooks.set(name, callback)},
  tool: {hook: () => undefined},
  event: {subscribe: async function* () {}},
});
const event = {
  sessionID: 'budget-exhausted-session',
  model: {providerID: 'opencode-go', id: 'beam-free'},
  agent: 'free-builder',
  tools: {read: {description: 'A real enabled tool'}},
  system: [],
};
try {
  await hooks.get('context')(event);
  assert.equal(Object.hasOwn(event, 'tools'), false,
    'providers must receive no tools field, not tools: []');
  assert.match(event.system.at(-1).text, /turn limit reached/);
  const runFiles = await (await import('node:fs/promises')).readdir(path.join(root, '.agent-runs'));
  assert.equal(runFiles.length, 1, 'the harness must retain the durable run record');
  const runPath = path.join(root, '.agent-runs', runFiles[0], 'run.json');
  const run = JSON.parse(readFileSync(runPath, 'utf8'));
  assert.equal(run.spec.model_id, 'opencode-go/beam-free');
} finally {
  rmSync(root, {recursive: true, force: true});
}
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout

