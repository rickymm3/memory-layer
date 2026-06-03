# memory-layer

A persistent, LLM-agnostic memory store that gives AI assistants durable memory across sessions, projects, and models.

---

## The problem

LLMs are stateless. Every chat session begins blank. If you've explained your project architecture, preferences, or decisions a hundred times, the next session knows none of it. And if you switch from Claude to Copilot to GPT-4, you start from zero again with each one.

memory-layer fixes this by maintaining a single shared knowledge store that any LLM can read from and write to — a shared brain that accumulates everything worth remembering.

---

## What it is

A local PostgreSQL + pgvector database with:

- **Semantic retrieval** — embed your query, find the most relevant stored facts
- **A write pipeline** — every candidate memory is reconciled against what's already stored, reviewed by a critic LLM, and risk-gated before anything is written
- **An MCP server** — connects to GitHub Copilot, Claude, and any MCP-compatible tool
- **A Flask dashboard** — browse atoms, signals, proposals, traces, and task history
- **A transcript ingest path** — import conversations from any LLM and extract what's worth keeping

---

## The shared brain vision

The goal is not just "compressed context for one chat." It's **LLM-agnostic persistent memory**:

- A conversation with GitHub Copilot in VS Code
- A conversation with Claude in a browser
- A conversation with a local Ollama model
- A future model that doesn't exist yet

All of them read and write to the **same store**. Decisions made in one conversation are available to all others. User preferences, project constraints, model-specific lessons — all in one place, under your control, on your hardware.

---

## Stack

| Component | Role |
|---|---|
| **PostgreSQL + pgvector** | Structured storage + cosine similarity search |
| **Ollama** | Local LLM for chat, extraction, reconciliation, and critique |
| **Python 3.12** | All application code |
| **MCP SDK** | Model Context Protocol server (stdio transport) |
| **Flask** | Read-only dashboard on port 5001 |
| **Docker Compose** | Runs PostgreSQL + pgvector locally |

Default models: `qwen3:8b` (chat/extraction/reconciliation), `qwen3-embedding:latest` (4096-dim embeddings).

Any Ollama model works. Any OpenAI-compatible API server works (LM Studio, llama.cpp, Jan, etc.).

---

## Quick start

### 1. Prerequisites

- Docker and Docker Compose
- Python 3.12+
- [Ollama](https://ollama.ai) running locally with your chosen model pulled

### 2. Clone and configure

```bash
git clone <this-repo>
cd memory-layer
cp .env.example .env
```

Edit `.env` — the minimum required settings:

```env
DATABASE_URL=postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development
OLLAMA_HOST=http://localhost:11434
CHAT_MODEL=qwen3:8b
EMBEDDING_MODEL=qwen3-embedding:latest
```

On WSL2 (Windows), Ollama runs on the Windows host — replace `localhost` with your gateway IP:
```env
OLLAMA_HOST=http://172.22.0.1:11434   # WSL2 only — run: ip route | grep default
```

See the **LLM provider setup** section below for Claude, OpenAI, and LM Studio configs.

### 3. Start the database

```bash
docker compose up -d
```

### 4. Install Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 5. Verify the stack

```bash
make doctor
# Expect: all PASS, 0 FAIL
```

### 6. Start a chat session

```bash
make session
```

---

## Connecting to GitHub Copilot (MCP)

Add this to `.vscode/mcp.json` in your project:

```json
{
  "servers": {
    "memoryLayer": {
      "type": "stdio",
      "command": "/path/to/memory-layer/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "envFile": "/path/to/memory-layer/.env"
    }
  }
}
```

VS Code launches the server as a subprocess over stdio — no port, no daemon.

Install the workflow instructions so Copilot knows when and how to use memory tools:

```bash
make install-vscode-prompts
```

---

## LLM provider setup

The memory layer separates chat and embeddings into two independent providers. Set them independently in `.env`.

> **Important — embedding model consistency**: once you store atoms with one embedding model, **do not change `EMBEDDING_MODEL`**. All stored vectors become incompatible. If you switch models, wipe the database and re-embed from scratch.

### Ollama only (default — no API keys needed)

```env
# No extra config needed. Defaults are:
OLLAMA_HOST=http://localhost:11434
CHAT_MODEL=qwen3:8b
EMBEDDING_MODEL=qwen3-embedding:latest
```

Pull the required models:
```bash
ollama pull qwen3:8b
ollama pull qwen3-embedding:latest
```

### Anthropic Claude (chat) + Ollama (embeddings)

Anthropic has no public embeddings API, so you still need Ollama running locally for embeddings.

```env
CHAT_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
CHAT_MODEL=claude-opus-4-5          # or claude-3-5-haiku-latest, etc.

EMBEDDING_PROVIDER=ollama           # required — Anthropic cannot embed
EMBEDDING_MODEL=qwen3-embedding:latest
OLLAMA_HOST=http://localhost:11434
```

### OpenAI API (chat + embeddings)

```env
CHAT_PROVIDER=openai
OPENAI_API_KEY=sk-...
CHAT_MODEL=gpt-4o

EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
```

### LM Studio / llama.cpp / Jan (local OpenAI-compatible server)

```env
CHAT_PROVIDER=openai_compat
OPENAI_COMPAT_BASE_URL=http://localhost:1234
OPENAI_COMPAT_API_KEY=none          # leave as "none" for local servers
CHAT_MODEL=your-loaded-model-name

EMBEDDING_PROVIDER=openai_compat
EMBEDDING_MODEL=your-embedding-model-name
```

### Claude Desktop (MCP)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "memory-layer": {
      "command": "/absolute/path/to/memory-layer/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "DATABASE_URL": "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development",
        "OLLAMA_HOST": "http://localhost:11434",
        "CHAT_MODEL": "qwen3:8b",
        "EMBEDDING_MODEL": "qwen3-embedding:latest"
      }
    }
  }
}
```

---

## Importing conversations from other LLMs

To bring memories from a Claude, GPT-4, or any other conversation into the store:

**Via CLI:**
```bash
# Export your conversation as JSON, then:
python scripts/ingest_transcript.py --file transcript.json --source claude-3-7-sonnet

# Dry-run first to see what would be extracted:
python scripts/ingest_transcript.py --file transcript.json --dry-run
```

**Transcript format** (JSON array):
```json
[
  {"role": "user", "content": "We decided to use Postgres, not SQLite."},
  {"role": "assistant", "content": "Got it, I'll remember that."},
  {"role": "user", "content": "Also, always use parameterized queries."}
]
```

**Via MCP** (from any connected LLM):
```
Call memory_ingest_transcript with the turns array and source_label="claude-3-7-sonnet"
```

Every imported candidate goes through the full pipeline — reconciliation, critic review, risk gate — before any write. Nothing is stored silently.

---

## Scope system

Scope is the isolation boundary between contexts:

| Scope | Meaning |
|---|---|
| `project:<slug>` | Facts for one project (e.g. `project:my-rails-app`) |
| `model:<name>` | Known behaviours/lessons for a specific model (e.g. `model:qwen3-8b`) |
| `user` | Cross-project user preferences and patterns |
| `global` | Universal facts surfaced in all contexts |

When you retrieve memories, both `project:<your-project>` and `user` scope are searched together — your personal preferences follow you into every project automatically.

---

## The write pipeline

Nothing is stored without passing through this pipeline:

```
Candidate memory
    │
    ├─ Reconcile: duplicate / refinement / conflict / new?
    │
    ├─ Critic LLM: durable? clear? safe? non-obvious?
    │
    ├─ Risk gate: commit / reinforce / propose / reject
    │
    └─ Write: memory_atom + memory_signal (single transaction)
              or → memory_proposals queue (requires human review)
```

**Auto-stored**: new facts and refinements that pass all checks.
**Queued for review**: conflicts, opinion changes, high-risk candidates.
**Rejected**: vague, obvious, sensitive, or temporary items.

---

## Dashboard

```bash
make dashboard
# Opens at http://localhost:5001
```

Browse: atoms, signals, proposals, context traces, response traces, task runs, commits, model lessons.

---

## Common commands

```bash
make session              # Interactive chat with memory retrieval + extraction
make dashboard            # Flask dashboard on port 5001
make doctor               # Health check (Postgres + Ollama + schema)
make list                 # Show recent memory atoms
make list-signals         # Show recent memory signals
make review-proposals     # Review and approve/reject pending proposals
make reflect ARGS="..."   # Post-task reflection (extract lessons from what just happened)
make mcp                  # Start MCP server manually

# Import a conversation transcript
python scripts/ingest_transcript.py --file transcript.json --source gpt-4o

# Direct writes
python scripts/store_memory.py "content" --type fact --scope project:myapp
python scripts/delete_memory.py <uuid>
python scripts/retrieve_memory.py "query string"
```

---

## Web research (optional)

Enable web search fallback for the dashboard chat:

```env
WEB_RESEARCH_ENABLED=true
WEB_SEARCH_PROVIDER=brave        # or: tavily
WEB_SEARCH_API_KEY=your_api_key
```

Web results are used in responses but are never auto-stored as memory atoms. Any durable lessons found via research must go through the write pipeline explicitly.

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development` | PostgreSQL connection string |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama server URL (WSL2: use Windows host IP) |
| `CHAT_PROVIDER` | `auto` | `auto` \| `ollama` \| `anthropic` \| `openai` \| `openai_compat` |
| `EMBEDDING_PROVIDER` | `auto` | `auto` \| `ollama` \| `openai` \| `openai_compat` (Anthropic excluded) |
| `CHAT_MODEL` | `qwen3:8b` | Model for chat, extraction, reconciliation |
| `EMBEDDING_MODEL` | `qwen3-embedding:latest` | Embedding model — **do not change after storing atoms** |
| `ANTHROPIC_API_KEY` | — | Required when `CHAT_PROVIDER=anthropic` |
| `OPENAI_API_KEY` | — | Required when `CHAT_PROVIDER=openai` or `EMBEDDING_PROVIDER=openai` |
| `OPENAI_COMPAT_BASE_URL` | — | Required when `CHAT_PROVIDER=openai_compat` |
| `OPENAI_COMPAT_API_KEY` | `none` | API key (use `none` for local servers) |
| `MEMORY_RETRIEVAL_THRESHOLD` | `0.60` | Minimum cosine similarity for retrieval |
| `WEB_RESEARCH_ENABLED` | `false` | Enable web search fallback |
| `WEB_SEARCH_PROVIDER` | `none` | `brave` or `tavily` |
| `WEB_SEARCH_API_KEY` | — | API key for the chosen web search provider |

---

## Security notes

- All data stays local — nothing leaves your machine unless you configure a hosted LLM or web search provider
- Web search queries are screened for sensitive content before being sent to external providers
- Raw web results are never auto-stored as memory atoms
- MCP write tools enforce the write pipeline — Copilot cannot bypass reconciliation or the risk gate
- Approval tokens for confirmed writes are single-use, expire in 5 minutes, and can only be generated by the local CLI

---

## Project status

Core pipeline, MCP server, Flask dashboard, signal aggregation, lifecycle management, web research, confidence-gated responses, and transcript ingest are all implemented and tested. See [docs/implementation_plan.md](docs/implementation_plan.md) for detailed phase history.
