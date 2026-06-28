#!/usr/bin/env node
'use strict';
/**
 * UserPromptSubmit hook — injects Synapse memory context before each turn.
 *
 * Reads the user's message from stdin (Claude Code hook JSON), calls
 * memory_task_context on the Synapse server, and writes the result to stdout
 * so Claude Code injects it into the conversation context.
 *
 * Env vars (set via `synapse setup` or your shell profile):
 *   MEMORY_LAYER_URL    e.g. http://192.168.1.10:5000/mcp/sse
 *   MEMORY_LAYER_TOKEN  your API token from /settings
 */

const https = require('https');
const http  = require('http');
const url   = require('url');

const SYNAPSE_URL   = process.env.MEMORY_LAYER_URL   || '';
const SYNAPSE_TOKEN = process.env.MEMORY_LAYER_TOKEN || '';

if (!SYNAPSE_URL || !SYNAPSE_TOKEN) process.exit(0);

let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => { raw += chunk; });
process.stdin.on('end', () => {
  let taskHint = '';
  try {
    const d = JSON.parse(raw);
    const msg = d.message || d.prompt || '';
    taskHint = String(msg).slice(0, 150).replace(/\n/g, ' ');
  } catch (_) {}

  const body = JSON.stringify({
    tool: 'memory_task_context',
    args: { project_scope: 'user', task_hint: taskHint, compact: true },
  });

  const parsed  = url.parse(SYNAPSE_URL);
  const isHttps = parsed.protocol === 'https:';

  const req = (isHttps ? https : http).request(
    {
      hostname: parsed.hostname,
      port:     parsed.port || (isHttps ? 443 : 80),
      path:     parsed.path,
      method:   'POST',
      headers: {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization':  `Bearer ${SYNAPSE_TOKEN}`,
      },
    },
    (res) => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        if (data) process.stdout.write(data);
        process.exit(0);
      });
    }
  );

  req.setTimeout(8000, () => { req.destroy(); process.exit(0); });
  req.on('error', () => process.exit(0));
  req.write(body);
  req.end();
});
