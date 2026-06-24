'use strict';
const { spawnSync, execFileSync } = require('child_process');
const os = require('os');

const VERSION = require('../package.json').version;
const IS_WIN  = os.platform() === 'win32';
const PYTHON  = IS_WIN ? 'python' : 'python3';
const EXTRA_ARGS = process.argv.slice(2);

// ── Hosted mode: MEMORY_LAYER_URL + MEMORY_LAYER_TOKEN are set ───────────────
// Claude Desktop passes these from claude_desktop_config.json env block.
// Run the JS bridge — no Python required.
if (process.env.MEMORY_LAYER_URL && process.env.MEMORY_LAYER_TOKEN) {
  require('./bridge');
  return;
}

// ── Self-hosted mode: run the Python stdio MCP server ────────────────────────

function which(cmd) {
  try { execFileSync(IS_WIN ? 'where' : 'which', [cmd], { stdio: 'ignore' }); return true; }
  catch { return false; }
}

function exec(cmd, args) {
  const result = spawnSync(cmd, args, { stdio: 'inherit', env: process.env });
  if (result.error) {
    process.stderr.write(`[memory-layer] Failed to start '${cmd}': ${result.error.message}\n`);
    process.exit(1);
  }
  process.exit(result.status ?? 0);
}

function isInstalled(python) {
  const result = spawnSync(python, ['-c', 'import mcp_server.server'], { stdio: 'ignore' });
  return result.status === 0;
}

if (which('uvx')) exec('uvx', [`memory-layer==${VERSION}`, ...EXTRA_ARGS]);
if (which('uv'))  exec('uv', ['tool', 'run', `memory-layer==${VERSION}`, ...EXTRA_ARGS]);

if (which(PYTHON)) {
  if (isInstalled(PYTHON)) exec(PYTHON, ['-m', 'mcp_server.server', ...EXTRA_ARGS]);

  process.stderr.write(`[memory-layer] Installing memory-layer==${VERSION} via pip (one-time setup)...\n`);
  const install = spawnSync(PYTHON, ['-m', 'pip', 'install', '--quiet', `memory-layer==${VERSION}`], { stdio: 'inherit' });
  if (install.status === 0) exec(PYTHON, ['-m', 'mcp_server.server', ...EXTRA_ARGS]);
  process.stderr.write('[memory-layer] pip install failed. See above for details.\n');
  process.exit(1);
}

process.stderr.write(`
[memory-layer] No Python found. Install uv for the fastest fix:
  macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh
  Windows:      powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
`);
process.exit(1);
