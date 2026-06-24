# memory-layer

Persistent shared memory for agentic workflows. Add one block to your MCP config — your AI agents remember everything across sessions, tools, and models.

## Install

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "memory-layer"],
      "env": {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

Restart Claude. Memory is stored at `~/.memory-layer/memory.db`. No Docker, no Postgres, no install step.

**Claude Code** — same block goes in `~/.claude/settings.json` under `mcpServers`.

## Requirements

Node.js 18+ (for `npx`). On first run, the package auto-detects your Python setup:

- **uv/uvx installed** → uses isolated environment automatically
- **Python 3.12+ installed** → installs memory-layer via pip (one-time)
- **Neither** → prints install instructions and exits cleanly

Install uv for the smoothest experience: https://docs.astral.sh/uv/

## Environment variables

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | For Anthropic chat |
| `OPENAI_API_KEY` | For OpenAI chat or embeddings |
| `CHAT_PROVIDER` | `anthropic` \| `openai` \| `ollama` (default: auto) |
| `EMBEDDING_PROVIDER` | `openai` \| `ollama` (default: auto) |
| `DATABASE_URL` | Set to `postgresql://...` for Postgres (default: SQLite) |

## Production (Postgres + shared memory)

```json
{
  "mcpServers": {
    "memory": {
      "command": "npx",
      "args": ["-y", "memory-layer"],
      "env": {
        "DATABASE_URL": "postgresql://user:pass@host:5432/dbname",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

## Links

- GitHub: https://github.com/rickymm3/memory-layer
- PyPI: https://pypi.org/project/memory-layer/
