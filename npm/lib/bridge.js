'use strict';
/**
 * Zero-dependency stdio MCP bridge for memory-layer.
 *
 * Claude Desktop runs this as a subprocess (stdio transport).
 * The bridge speaks MCP JSON-RPC 2.0 on stdin/stdout and forwards
 * tool calls to the Synapse site via HTTP POST to MEMORY_LAYER_URL.
 *
 * Required env vars (set by claude_desktop_config.json):
 *   MEMORY_LAYER_URL    e.g. http://localhost:5000/mcp/sse
 *   MEMORY_LAYER_TOKEN  the user's api_token from /settings
 */

const https = require('https');
const http = require('http');
const url = require('url');

const BASE_URL = (process.env.MEMORY_LAYER_URL || '').replace(/\/+$/, '');
const TOKEN    = process.env.MEMORY_LAYER_TOKEN || '';

if (!BASE_URL || !TOKEN) {
  process.stderr.write('[memory-layer] MEMORY_LAYER_URL and MEMORY_LAYER_TOKEN must be set.\n');
  process.exit(1);
}

// ── Tool definitions (mirrors mcp_server/server.py) ───────────────────────────

const TOOLS = [
  {
    name: 'memory_health',
    description: 'Check memory-layer health: DB reachability, Ollama reachability, atom count.',
    annotations: { title: 'Memory Health Check', readOnlyHint: true },
    inputSchema: { type: 'object', properties: {}, required: [] },
  },
  {
    name: 'memory_search',
    description: 'Search memory atoms by semantic similarity.',
    annotations: { title: 'Search Memories', readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        query:          { type: 'string',  description: 'Natural language query.' },
        limit:          { type: 'integer', description: 'Max results (1-20). Default 5.' },
        scope:          { type: 'string',  description: 'Optional scope filter.' },
        memory_type:    { type: 'string',  description: 'Optional type filter.' },
        min_similarity: { type: 'number',  description: 'Minimum cosine similarity 0-1.' },
      },
      required: ['query'],
    },
  },
  {
    name: 'memory_store_auto',
    description: 'Store a memory atom through the full commit pipeline.',
    annotations: { title: 'Store Memory', readOnlyHint: false, destructiveHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        content:       { type: 'string', description: 'Full canonical sentence to store.' },
        memory_type:   { type: 'string', description: 'fact | decision | instruction | observation | preference | correction' },
        relationship:  { type: 'string', description: 'new | refinement | reinforcement | conflict | opinion_change' },
        context_summary: { type: 'string' },
        scope:         { type: 'string' },
        confidence:    { type: 'number' },
        importance:    { type: 'number' },
        visibility:    { type: 'string', description: 'public | private | team. Default public — only use private for passwords, PII, or sensitive personal details.' },
      },
      required: ['content', 'memory_type', 'relationship'],
    },
  },
  {
    name: 'memory_get',
    description: 'Fetch a single memory atom by UUID.',
    annotations: { title: 'Get Memory by ID', readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: { memory_id: { type: 'string', description: 'UUID of the atom.' } },
      required: ['memory_id'],
    },
  },
  {
    name: 'memory_task_context',
    description: 'Session-start snapshot: project context + model lessons + task history.',
    annotations: { title: 'Load Session Context', readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        project_scope: { type: 'string' },
        model_scope:   { type: 'string' },
        task_hint:     { type: 'string' },
        recent_tasks:  { type: 'integer' },
        compact:       { type: 'boolean' },
      },
      required: ['project_scope'],
    },
  },
  {
    name: 'memory_audit',
    description: 'Compound corpus health report: stale atoms + near-duplicates + stats.',
    annotations: { title: 'Audit Memory Corpus', readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        scope:                { type: 'string'  },
        stale_days:           { type: 'integer' },
        duplicate_threshold:  { type: 'number'  },
      },
      required: [],
    },
  },
  {
    name: 'memory_link_atoms',
    description: 'Create an explicit directed relation between two atoms.',
    annotations: { title: 'Link Two Memories', readOnlyHint: false, destructiveHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        atom_a_id:    { type: 'string' },
        atom_b_id:    { type: 'string' },
        relation_type: { type: 'string' },
        confidence:   { type: 'number' },
      },
      required: ['atom_a_id', 'atom_b_id'],
    },
  },
  {
    name: 'memory_related',
    description: 'Traverse the atom relations graph from a starting atom.',
    annotations: { title: 'Find Related Memories', readOnlyHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        atom_id:       { type: 'string'  },
        depth:         { type: 'integer' },
        relation_types: { type: 'array', items: { type: 'string' } },
      },
      required: ['atom_id'],
    },
  },
  {
    name: 'memory_push_conversation',
    description: 'Push an entire conversation into memory atoms. Post drafts are generated automatically — check /drafts for suggested posts.',
    annotations: { title: 'Ingest Conversation', readOnlyHint: false, destructiveHint: true },
    inputSchema: {
      type: 'object',
      properties: {
        transcript:    { type: 'string',  description: 'Conversation text with User:/Assistant: markers, or a .jsonl file path.' },
        is_jsonl_path: { type: 'boolean', description: 'Set true if transcript is a path to a Claude Code session .jsonl file.' },
      },
      required: ['transcript'],
    },
  },
];

// ── Prompt definitions (handled locally — no server round-trip needed) ───────

const PROMPTS = [
  {
    name: 'start_session',
    description: 'Start a memory-enabled session: consent disclosure + load your Synapse context.',
    arguments: [],
  },
  {
    name: 'push_to_synapse',
    description: 'Retroactively push this conversation to Synapse memory. Use when you forgot to start a session.',
    arguments: [],
  },
];

const PROMPT_CONTENT = {
  start_session: [
    {
      role: 'user',
      content: {
        type: 'text',
        text: [
          '🧠 **Starting a Synapse memory session**',
          '',
          'Before we begin — a quick heads-up on what this connection does:',
          '',
          '• What you share may be stored as memory atoms (your beliefs, preferences, decisions)',
          '• High-quality atoms can contribute to public posts on Synapse, visible to other users',
          '• Passwords, API keys, and private personal details are kept private automatically',
          '• You can say "keep this private" at any point to exclude something from storage',
          '',
          'Selecting this prompt means you\'re good with this for our conversation.',
          'If you want to push a conversation retroactively instead, use /push_to_synapse.',
          '',
          '---',
          '',
          'Please call memory_task_context now with:',
          '  project_scope = "project:memory-layer"',
          '  model_scope   = "model:claude-sonnet-4-6"',
          '  task_hint     = "general session — user will direct"',
          '',
          'Then confirm you\'re loaded and ready.',
        ].join('\n'),
      },
    },
  ],
  push_to_synapse: [
    {
      role: 'user',
      content: {
        type: 'text',
        text: [
          '📤 **Push this conversation to Synapse**',
          '',
          'You\'re about to send this conversation retroactively to the Synapse memory layer.',
          '',
          'What will happen:',
          '• The conversation is analyzed for durable memories — beliefs, decisions, preferences',
          '• Extracted atoms are stored as public by default',
          '• Passwords and private details are automatically kept private',
          '• Atoms may contribute to or update public posts on Synapse',
          '',
          'By proceeding you\'re consenting to this for the current conversation.',
          '',
          '---',
          '',
          'Please call memory_push_conversation now.',
          'Pass the conversation transcript as the "transcript" argument.',
          'Set is_jsonl_path = false.',
        ].join('\n'),
      },
    },
  ],
};

// ── HTTP call to Synapse site ─────────────────────────────────────────────────

function callSite(tool, args) {
  return new Promise((resolve, reject) => {
    const parsed  = url.parse(BASE_URL);
    const isHttps = parsed.protocol === 'https:';
    const body    = JSON.stringify({ tool, args });
    const options = {
      hostname: parsed.hostname,
      port:     parsed.port || (isHttps ? 443 : 80),
      path:     parsed.path,
      method:   'POST',
      headers: {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization':  `Bearer ${TOKEN}`,
      },
    };
    const req = (isHttps ? https : http).request(options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          const parsed = JSON.parse(data);
          if (parsed.error) reject(new Error(parsed.error));
          else resolve(parsed.result);
        } catch (e) {
          reject(new Error(`Invalid JSON from server: ${data.slice(0, 200)}`));
        }
      });
    });
    req.on('error', reject);
    req.setTimeout(30000, () => { req.destroy(); reject(new Error('Request timed out')); });
    req.write(body);
    req.end();
  });
}

// ── MCP JSON-RPC 2.0 stdio server ────────────────────────────────────────────

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + '\n');
}

function error(id, code, message) {
  send({ jsonrpc: '2.0', id, error: { code, message } });
}

async function handle(msg) {
  const { id, method, params } = msg;

  if (method === 'initialize') {
    send({
      jsonrpc: '2.0', id,
      result: {
        protocolVersion: '2024-11-05',
        capabilities: { tools: {}, prompts: {} },
        serverInfo: { name: 'memoryLayer', version: '0.1.0' },
        instructions: [
          'MANDATORY PER-TURN WRITE RULE: After any turn where the user expresses a',
          'preference, correction, decision, or instruction — call memory_store_auto',
          'BEFORE finishing your response. Do not defer to end-of-session.',
          'Scope rules: project facts → scope="project:<name>",',
          'observations about your own behavior → scope="model:claude-sonnet-4-6",',
          'user preferences → scope="user".',
          'Report both memory_atom_id and memory_signal_id for every write.',
          'Triggers: preferences with reasons, architecture decisions, corrections,',
          'frustration or satisfaction signals, facts you would otherwise forget.',
        ].join(' '),
      },
    });
    return;
  }

  if (method === 'notifications/initialized') return; // no response

  if (method === 'ping') {
    send({ jsonrpc: '2.0', id, result: {} });
    return;
  }

  if (method === 'tools/list') {
    send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
    return;
  }

  if (method === 'prompts/list') {
    send({ jsonrpc: '2.0', id, result: { prompts: PROMPTS } });
    return;
  }

  if (method === 'prompts/get') {
    const name = params && params.name;
    const content = name && PROMPT_CONTENT[name];
    if (!content) {
      error(id, -32602, `Unknown prompt: ${name}`);
      return;
    }
    const prompt = PROMPTS.find(p => p.name === name);
    send({
      jsonrpc: '2.0', id,
      result: {
        description: prompt ? prompt.description : name,
        messages: content,
      },
    });
    return;
  }

  if (method === 'tools/call') {
    const toolName = params && params.name;
    const toolArgs = (params && params.arguments) || {};
    if (!toolName) { error(id, -32602, 'Missing tool name'); return; }

    try {
      const result = await callSite(toolName, toolArgs);
      send({
        jsonrpc: '2.0', id,
        result: {
          content: [{ type: 'text', text: JSON.stringify(result, null, 2) }],
        },
      });
    } catch (e) {
      send({
        jsonrpc: '2.0', id,
        result: {
          content: [{ type: 'text', text: `Error: ${e.message}` }],
          isError: true,
        },
      });
    }
    return;
  }

  // Unknown method
  if (id !== undefined) error(id, -32601, `Method not found: ${method}`);
}

// ── Main: read newline-delimited JSON from stdin ──────────────────────────────

let buf = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => {
  buf += chunk;
  const lines = buf.split('\n');
  buf = lines.pop(); // keep incomplete last line
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    let msg;
    try { msg = JSON.parse(trimmed); } catch { continue; }
    handle(msg).catch(e => process.stderr.write(`[memory-layer] ${e.message}\n`));
  }
});

process.stdin.on('end', () => process.exit(0));
process.on('SIGINT',  () => process.exit(0));
process.on('SIGTERM', () => process.exit(0));
