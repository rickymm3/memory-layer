#!/usr/bin/env node
'use strict';
/**
 * LOAD — runs outside the model, so a new context window starts with memory
 * whether or not the model decides to call a tool.
 *
 *   SessionStart      → memory_task_context for this project: project facts,
 *                       user preferences, model lessons, contested claims.
 *                       Also fires after compaction (source=compact), which is
 *                       exactly when the previous window's context is gone.
 *   UserPromptSubmit  → only the atoms relevant to this prompt.
 */

const { configured, readStdin, projectScope, callTool, emitContext } = require('./lib');

const MAX_PER_SECTION = 8;

function section(title, items) {
  if (!Array.isArray(items) || items.length === 0) return [];
  return [`### ${title}`, ...items.slice(0, MAX_PER_SECTION).map(String), ''];
}

async function main() {
  if (!configured()) return;
  const input = await readStdin();
  const event = input.hook_event_name || process.argv[2] || 'SessionStart';
  const scope = projectScope(input.cwd);

  if (event === 'UserPromptSubmit') {
    const prompt = String(input.prompt || '').slice(0, 300).replace(/\s+/g, ' ').trim();
    if (prompt.length < 12) return; // "ok", "thanks", "go"
    const res = await callTool('memory_task_context', {
      project_scope: scope,
      model_scope: process.env.SYNAPSE_MODEL_SCOPE || null,
      task_hint: prompt,
      recent_tasks: 1,
      compact: true,
    }, 6000);
    const lines = section('Memory relevant to this prompt', res && res.task_relevant_atoms);
    if (lines.length) emitContext(event, ['## MEMORY — RELEVANT', ...lines].join('\n'));
    return;
  }

  const res = await callTool('memory_task_context', {
    project_scope: scope,
    model_scope: process.env.SYNAPSE_MODEL_SCOPE || null,
    compact: true,
  }, 8000);
  if (!res) return;
  const lines = [
    `## MEMORY — SESSION CONTEXT (auto-loaded for ${scope})`,
    'Durable memory from earlier sessions. CONTESTED items are disagreements:',
    'report each claim with who made it instead of picking one.',
    '',
    ...section('Project', res.project_context),
    ...section('User preferences', res.user_context),
    ...section('Model lessons', res.model_lessons),
    ...section('Recent tasks', res.recent_task_runs),
  ];
  if (res.write_protocol) lines.push('### Saving', res.write_protocol);
  emitContext('SessionStart', lines.join('\n'));
}

main().catch(() => process.exit(0)).then(() => process.exit(0));
