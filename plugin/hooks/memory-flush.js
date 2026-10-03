#!/usr/bin/env node
'use strict';
/**
 * SAVE — runs outside the model at the two moments a context window's content
 * is about to be lost:
 *
 *   PreCompact  → the window is about to be summarized
 *   SessionEnd  → the session is closing
 *
 * Sends the turns written since the last flush to memory_push_conversation,
 * which extracts candidates and runs each through the commit pipeline
 * (reconcile → critic → risk gate). Atoms land private, scoped to this project
 * unless the extractor scoped them itself (e.g. scope='user').
 *
 * The push runs in a detached child so the hook returns immediately.
 */

const fs = require('fs');
const { spawn } = require('child_process');
const { configured, readStdin, projectScope, callTool, statePath, STATE_DIR } = require('./lib');

const MAX_CHARS = 60000;

function textOf(content) {
  if (typeof content === 'string') return content;
  if (!Array.isArray(content)) return '';
  return content
    .filter((b) => b && b.type === 'text' && typeof b.text === 'string')
    .map((b) => b.text)
    .join('\n');
}

/** Returns { transcript, lineCount } for lines after `fromLine`. */
function readNewTurns(transcriptPath, fromLine) {
  const lines = fs.readFileSync(transcriptPath, 'utf8').split('\n');
  const out = [];
  for (let i = fromLine; i < lines.length; i++) {
    if (!lines[i].trim()) continue;
    let d;
    try { d = JSON.parse(lines[i]); } catch (_) { continue; }
    if (d.isSidechain || d.isMeta) continue;
    const msg = d.message || {};
    const role = msg.role || d.type;
    if (role !== 'user' && role !== 'assistant') continue;
    const text = textOf(msg.content).trim();
    if (!text || text.startsWith('<command-') || text.startsWith('<local-command')) continue;
    out.push(`${role === 'user' ? 'User' : 'Assistant'}: ${text}`);
  }
  let transcript = out.join('\n\n');
  if (transcript.length > MAX_CHARS) transcript = transcript.slice(-MAX_CHARS);
  return { transcript, lineCount: lines.length, hasUser: out.some((l) => l.startsWith('User:')) };
}

async function worker(argPath) {
  const job = JSON.parse(fs.readFileSync(argPath, 'utf8'));
  fs.unlinkSync(argPath);
  await callTool('memory_push_conversation', {
    transcript: job.transcript,
    is_jsonl_path: false,
    scope: job.scope,
  }, 10 * 60 * 1000);
  fs.writeFileSync(job.offsetFile, String(job.lineCount));
}

async function main() {
  if (process.argv[2] === '--worker') return worker(process.argv[3]);
  if (!configured()) return;

  const input = await readStdin();
  const { session_id: sessionId, transcript_path: transcriptPath } = input;
  if (!sessionId || !transcriptPath || !fs.existsSync(transcriptPath)) return;

  fs.mkdirSync(STATE_DIR, { recursive: true });
  const offsetFile = statePath(sessionId, 'flushed');
  let fromLine = 0;
  try { fromLine = parseInt(fs.readFileSync(offsetFile, 'utf8'), 10) || 0; } catch (_) {}

  const { transcript, lineCount, hasUser } = readNewTurns(transcriptPath, fromLine);
  if (!hasUser) return;

  const jobFile = statePath(sessionId, `job-${Date.now()}.json`);
  fs.writeFileSync(jobFile, JSON.stringify({
    transcript, lineCount, offsetFile, scope: projectScope(input.cwd),
  }));
  const child = spawn(process.execPath, [__filename, '--worker', jobFile], {
    detached: true, stdio: 'ignore', env: process.env,
  });
  child.unref();
}

main().catch(() => process.exit(0)).then(() => process.exit(0));
