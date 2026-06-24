# memory-layer

Persistent shared memory for agentic workflows. Drop one block into your MCP config — your AI agents remember everything across sessions, tools, and models.

---

## Install

Add to your Claude Desktop config and restart:

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

Memory is stored at `~/.memory-layer/memory.db`. No Docker, no Postgres, no install step. Requires Node.js 18+.

**Claude Desktop config location:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

**Claude Code** — same block in `~/.claude/settings.json` under `mcpServers`.

**Python/uv users** (alternative, no Node.js required):
```json
{
  "mcpServers": {
    "memory": {
      "command": "uvx",
      "args": ["memory-layer"],
      "env": { "ANTHROPIC_API_KEY": "sk-ant-...", "OPENAI_API_KEY": "sk-..." }
    }
  }
}
```

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Anthropic chat |
| `OPENAI_API_KEY` | — | Required for OpenAI chat or embeddings |
| `CHAT_PROVIDER` | `auto` | `anthropic` \| `openai` \| `openai_compat` \| `ollama` |
| `EMBEDDING_PROVIDER` | `auto` | `openai` \| `openai_compat` \| `ollama` |
| `EMBEDDING_MODEL` | `qwen3-embedding:latest` | Any model your provider supports |
| `DATABASE_URL` | SQLite at `~/.memory-layer/memory.db` | Set to `postgresql://...` for Postgres |
| `SQLITE_DB_PATH` | `~/.memory-layer/memory.db` | Override the SQLite path |
| `OLLAMA_HOST` | auto-detected | `http://localhost:11434` for local Ollama |

---

## Upgrade to Postgres (production / shared)

For team use or when you need shared memory across machines:

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

Or with uvx + postgres extra:
```json
{
  "mcpServers": {
    "memory": {
      "command": "uvx",
      "args": ["--with", "memory-layer[postgres]", "memory-layer"],
      "env": {
        "DATABASE_URL": "postgresql://user:pass@host:5432/dbname",
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "OPENAI_API_KEY": "sk-..."
      }
    }
  }
}
```

---

## What it does

memory-layer exposes 20 MCP tools that let any LLM read from and write to a shared knowledge store:

- **Retrieval** — `memory_search`, `memory_project_context`, `memory_task_context`
- **Storage** — `memory_store_auto` (low-risk, direct write), `memory_propose_signal` (high-risk, queued for review)
- **Reflection** — `memory_reflect_turn`, `memory_ingest_transcript`
- **Audit** — `memory_get_belief`, `memory_get_signals`, `memory_health`

Every write is dual: one `memory_atom` (the canonical belief) + one `memory_signal` (the evidence that produced it). Signals aggregate into confidence scores; contested beliefs are flagged automatically.

---

## Dev / self-host

```bash
git clone https://github.com/rickymm3/memory-layer.git
cd memory-layer
cp .env.example .env   # edit with your keys
pip install -e ".[dev]"
make doctor            # verify stack
make session           # interactive chat with memory
make dashboard         # read-only UI on :5001
```

For Postgres: `docker compose up -d` then set `DATABASE_URL` in `.env`.
