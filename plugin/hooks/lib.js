'use strict';
/**
 * Shared helpers for the Synapse hooks: config, scope detection, and the
 * HTTP call to the server's /mcp/sse tool dispatcher.
 *
 * Env vars (set via `node setup.js` or your shell profile):
 *   MEMORY_LAYER_URL        e.g. http://192.168.1.10:5000/mcp/sse
 *   MEMORY_LAYER_TOKEN      your API token from /settings
 *   SYNAPSE_PROJECT_SCOPE   optional override, e.g. project:billing-api
 *   SYNAPSE_MODEL_SCOPE     optional, e.g. model:claude-sonnet-4-6
 */

const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const https = require('https');
const { execFileSync } = require('child_process');

const SYNAPSE_URL = process.env.MEMORY_LAYER_URL || '';
const SYNAPSE_TOKEN = process.env.MEMORY_LAYER_TOKEN || '';
const STATE_DIR = path.join(os.homedir(), '.synapse', 'sessions');

function configured() {
  return Boolean(SYNAPSE_URL && SYNAPSE_TOKEN);
}

function readStdin() {
  return new Promise((resolve) => {
    let raw = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { raw += c; });
    process.stdin.on('end', () => {
      try { resolve(JSON.parse(raw)); } catch (_) { resolve({}); }
    });
  });
}

/** project:<repo name> for the session's working directory. */
function projectScope(cwd) {
  if (process.env.SYNAPSE_PROJECT_SCOPE) return process.env.SYNAPSE_PROJECT_SCOPE;
  const dir = cwd || process.cwd();
  let root = dir;
  try {
    root = execFileSync('git', ['rev-parse', '--show-toplevel'], {
      cwd: dir, stdio: ['ignore', 'pipe', 'ignore'], timeout: 2000,
    }).toString().trim() || dir;
  } catch (_) { /* not a git repo */ }
  const name = path.basename(root).toLowerCase().replace(/[^a-z0-9._-]+/g, '-');
  return `project:${name || 'default'}`;
}

function callTool(tool, args, timeoutMs) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ tool, args });
    const u = new URL(SYNAPSE_URL);
    const client = u.protocol === 'https:' ? https : http;
    const req = client.request(
      {
        hostname: u.hostname,
        port: u.port || (u.protocol === 'https:' ? 443 : 80),
        path: u.pathname + u.search,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          Authorization: `Bearer ${SYNAPSE_TOKEN}`,
        },
      },
      (res) => {
        let data = '';
        res.on('data', (c) => { data += c; });
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            if (parsed.error) reject(new Error(parsed.error));
            else resolve(parsed.result);
          } catch (e) { reject(e); }
        });
      }
    );
    req.setTimeout(timeoutMs, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

/** Print additionalContext for SessionStart / UserPromptSubmit / PreCompact. */
function emitContext(event, text) {
  if (!text) return;
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: { hookEventName: event, additionalContext: text },
  }));
}

function statePath(sessionId, suffix) {
  return path.join(STATE_DIR, `${String(sessionId).replace(/[^\w.-]/g, '_')}.${suffix}`);
}

module.exports = {
  configured, readStdin, projectScope, callTool, emitContext, statePath, STATE_DIR,
};
