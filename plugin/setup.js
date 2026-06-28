#!/usr/bin/env node
'use strict';
/**
 * synapse setup — interactive configuration for the Synapse Claude Code plugin.
 *
 * Writes MEMORY_LAYER_URL and MEMORY_LAYER_TOKEN to:
 *   ~/.synapse/config          (env file sourced by the plugin)
 *   ~/.bashrc / ~/.zshrc       (optional, so vars are always available)
 *
 * Run:  node setup.js
 *   or: npx synapse-plugin setup   (once published)
 */

const fs       = require('fs');
const path     = require('path');
const os       = require('os');
const readline = require('readline');
const https    = require('https');
const http     = require('http');
const url      = require('url');

const CONFIG_DIR  = path.join(os.homedir(), '.synapse');
const CONFIG_FILE = path.join(CONFIG_DIR, 'config');

function ask(rl, question, defaultVal) {
  return new Promise(resolve => {
    const prompt = defaultVal ? `${question} [${defaultVal}]: ` : `${question}: `;
    rl.question(prompt, answer => resolve(answer.trim() || defaultVal || ''));
  });
}

function testConnection(synapseUrl, token) {
  return new Promise(resolve => {
    const body = JSON.stringify({ tool: 'memory_health', args: {} });
    const parsed  = url.parse(synapseUrl);
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
          'Authorization':  `Bearer ${token}`,
        },
      },
      (res) => {
        let data = '';
        res.on('data', chunk => { data += chunk; });
        res.on('end', () => {
          try {
            const parsed = JSON.parse(data);
            resolve({ ok: !parsed.error, data: parsed });
          } catch {
            resolve({ ok: false, data });
          }
        });
      }
    );
    req.setTimeout(6000, () => { req.destroy(); resolve({ ok: false, data: 'timeout' }); });
    req.on('error', e => resolve({ ok: false, data: e.message }));
    req.write(body);
    req.end();
  });
}

async function main() {
  console.log('\nSynapse plugin setup\n' + '─'.repeat(40));
  console.log('This connects Claude Code to your Synapse memory server.');
  console.log('Get your token from the /settings page on your Synapse site.\n');

  const existing = {};
  try {
    fs.readFileSync(CONFIG_FILE, 'utf8').split('\n').forEach(line => {
      const [k, ...v] = line.split('=');
      if (k && v.length) existing[k.trim()] = v.join('=').trim();
    });
  } catch (_) {}

  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });

  const synapseUrl = await ask(
    rl,
    'Synapse server URL (include /mcp/sse path)',
    existing.MEMORY_LAYER_URL || 'http://localhost:5000/mcp/sse'
  );
  const token = await ask(
    rl,
    'API token (from Synapse /settings)',
    existing.MEMORY_LAYER_TOKEN || ''
  );

  rl.close();

  if (!synapseUrl || !token) {
    console.error('\nURL and token are required. Run setup again.');
    process.exit(1);
  }

  // Test the connection before saving
  process.stdout.write('\nTesting connection...');
  const result = await testConnection(synapseUrl, token);
  if (result.ok) {
    console.log(' ✓ Connected');
  } else {
    console.log(` ✗ Failed (${JSON.stringify(result.data)})`);
    console.log('Saving anyway — check your URL and token if the plugin does not work.');
  }

  // Write ~/.synapse/config
  if (!fs.existsSync(CONFIG_DIR)) fs.mkdirSync(CONFIG_DIR, { mode: 0o700 });
  const configContent = [
    `MEMORY_LAYER_URL=${synapseUrl}`,
    `MEMORY_LAYER_TOKEN=${token}`,
  ].join('\n') + '\n';
  fs.writeFileSync(CONFIG_FILE, configContent, { mode: 0o600 });
  console.log(`\nConfig saved to ${CONFIG_FILE}`);

  // Optionally append to shell profile
  const shell   = process.env.SHELL || '';
  const profile = shell.includes('zsh')
    ? path.join(os.homedir(), '.zshrc')
    : path.join(os.homedir(), '.bashrc');

  const exportLines = [
    `export MEMORY_LAYER_URL="${synapseUrl}"`,
    `export MEMORY_LAYER_TOKEN="${token}"`,
  ];

  let profileContent = '';
  try { profileContent = fs.readFileSync(profile, 'utf8'); } catch (_) {}

  const alreadySet = exportLines.every(line => profileContent.includes(line));
  if (!alreadySet) {
    const block = '\n# Synapse memory layer\n' + exportLines.join('\n') + '\n';
    fs.appendFileSync(profile, block);
    console.log(`Shell exports appended to ${profile}`);
    console.log('Run:  source ' + profile + '  (or open a new terminal)');
  } else {
    console.log('Shell profile already contains Synapse config — skipping.');
  }

  console.log('\nSetup complete. Test with:');
  console.log(`  claude --plugin-dir ${path.resolve(__dirname)}`);
  console.log('  /synapse:recall <anything you\'ve discussed before>\n');
}

main().catch(e => { console.error(e.message); process.exit(1); });
