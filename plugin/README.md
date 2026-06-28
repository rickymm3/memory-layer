# Synapse — Claude Code Plugin

Connects Claude Code (and Claude Desktop) to your Synapse memory layer. Once configured, Claude remembers your preferences, decisions, and context across every conversation and surfaces them automatically.

## What it includes

| Component | What it does |
|---|---|
| **MCP server** | Exposes `memory_store_auto`, `memory_search`, `memory_task_context`, and other tools to Claude |
| **Session hook** | Injects relevant memory context at the start of each conversation turn |
| `/synapse:recall` | Skill to explicitly search past decisions and preferences |

## Quick start

### 1. Run setup

```bash
cd plugin
node setup.js
```

This prompts for your Synapse server URL and API token, tests the connection, and writes `~/.synapse/config` + shell profile exports.

Get your token from the `/settings` page on your Synapse site after logging in.

### 2. Load the plugin

```bash
# From the plugin directory:
claude --plugin-dir /path/to/memory-layer/plugin

# Or from anywhere, pointing at the plugin folder:
claude --plugin-dir ~/memory-layer/plugin
```

### 3. Verify it works

```
/synapse:recall what projects am I working on
```

---

## Configuration

The plugin reads two env vars:

| Variable | Description |
|---|---|
| `MEMORY_LAYER_URL` | Full URL to your Synapse MCP endpoint, e.g. `http://192.168.1.10:5000/mcp/sse` |
| `MEMORY_LAYER_TOKEN` | Your API token from Synapse `/settings` |

`node setup.js` writes these to `~/.synapse/config` and appends exports to your shell profile. If you prefer to set them manually, add to `~/.bashrc` or `~/.zshrc`:

```bash
export MEMORY_LAYER_URL="http://your-synapse-server:5000/mcp/sse"
export MEMORY_LAYER_TOKEN="your-api-token"
```

---

## Using on another machine (e.g. MacBook)

1. Ensure your Synapse server is reachable on the network (e.g. `http://192.168.1.10:5000`)
2. Clone this repo or copy the `plugin/` folder to the new machine
3. Run `node setup.js` and enter the network URL + your token
4. Load with `claude --plugin-dir /path/to/plugin`

Your memory is stored server-side — switching machines just means pointing at the same server.

---

## Claude Desktop (Windows)

Claude Desktop uses `claude_desktop_config.json` for MCP, not the plugin system. See the **Settings** page on your Synapse site for the pre-filled config block.

For Windows users where `npx` isn't in PATH, use the WSL variant documented on the Settings page.

---

## Development

After editing any plugin files:

```
/reload-plugins
```

to pick up changes in the current session.
