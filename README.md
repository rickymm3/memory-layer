# Synapse — Demand-Driven AI Memory Network

A persistent memory layer for LLMs that evolves into a federated knowledge network. Conversations across any AI surface accumulate structured beliefs in a shared Postgres store. When the AI lacks confidence, it routes questions to relevant humans — turning knowledge gaps into targeted forum-style posts answered by locals, experts, and hobbyists.

---

## What it is today

A production-ready memory backend that plugs into Claude Desktop, VS Code, and the Synapse hosted site. Every conversation writes structured memory atoms into Postgres. Future conversations retrieve the most relevant atoms via semantic search and inject them into the AI's context — giving any LLM durable, reconciled memory across sessions, tools, and models.

**Three surfaces are live and writing to the same database:**
- Claude Desktop (MCP stdio)
- Claude Code / VS Code (MCP via `.claude/settings.json`)
- Synapse site (`make site` → `/chat`, admin-only)

---

## Architecture

### Memory store

Every piece of knowledge is stored as a **memory atom** — a canonical belief with:
- `content` — the full claim
- `memory_type` — `fact | decision | instruction | observation | preference | correction`
- `scope` — `project:<name> | model:<id> | user` — determines retrieval context
- `confidence` and `importance` — float 0–1
- `visibility` — `private | team | public`
- `lifecycle_status` — `active | contested | superseded`
- `embedding` — 4096-dim vector via `qwen3-embedding` (Ollama, local)

Every write also creates a **memory signal** — the evidence event that produced or modified the atom. Signals aggregate into `support_weight` and `opposition_weight`, which drive automatic confidence recomputation and `disagreement_score`. No atom is ever silently overwritten — history is preserved.

### Write pipeline

All writes (from any surface) go through the same commit pipeline:

```
content → write-quality scoring → reconciliation against existing atoms
        → critic review (LLM) → risk gate → Postgres write
        → memory_atom + memory_signal (dual-write, always)
```

Contested beliefs queue for signal-based resolution rather than human review. The pipeline rejects low-quality writes, merges reinforcements, and flags conflicts automatically.

### Chat pipeline (`app/chat.py`)

Each chat turn:
1. Retrieves top-K atoms via cosine similarity (pgvector, exact search)
2. Evaluates context quality (sufficient / conflicting / stale)
3. Routes: **direct** (no memory, answer from training) | **context** (inject and answer) | **recursive** (contested atom → draft → evaluate → web search → revise)
4. Runs post-turn reflection in background thread → extracts candidates → commit pipeline

### MCP server (`mcp_server/server.py`)

8 tools exposed via stdio and SSE/HTTP:

| Tool | Purpose |
|---|---|
| `memory_health` | DB + Ollama reachability, atom count |
| `memory_search` | Semantic similarity search with scope/type filters |
| `memory_store_auto` | Full commit pipeline write |
| `memory_get` | Fetch single atom by UUID |
| `memory_task_context` | Session-start snapshot: project + model lessons + task history |
| `memory_audit` | Compound health + stale + duplicate report |
| `memory_link_atoms` | Create explicit relation between atoms |
| `memory_related` | Traverse atom relations graph (1–3 hops) |

Transport: `stdio` (default, for Claude Desktop / Claude Code) or `SSE/HTTP` (`MCP_TRANSPORT=sse`, port 8765, Bearer token auth).

---

## Synapse hosted site (`webapp/`)

Flask Blueprint (`app_main.py`) with:

- **Auth**: signup, login, per-user `api_token` for MCP/REST auth
- **Brain** (`/brain`): user's own memory atoms
- **Discussions** (`/discussions`): auto-clustered threads from public atoms, with novelty scoring and unread notifications
- **Knowledge graph** (`/discussions/graph`): D3 force-directed canvas, Obsidian-style
- **Feed** (`/feed`): published posts from the AI-driven article pipeline
- **Admin** (`/admin`): full atom browser, visibility toggles, user management
- **Chat** (`/chat`, admin): memory-augmented chat — same pipeline as the dashboard
- **Capture** (`/capture`): manual atom submission
- **Settings** (`/settings`): API token + MCP config snippets

REST ingest: `POST /api/ingest` (Bearer token, JSON body) — the universal write endpoint for non-MCP clients.

MCP over HTTP: `POST /mcp/sse` — tool dispatcher used by the npm bridge.

---

## Integrations

### Claude Desktop (Windows/WSL)

```json
{
  "mcpServers": {
    "memoryLayer": {
      "command": "wsl",
      "args": ["-e", "/home/ricky/memory-layer/.venv/bin/python",
               "/home/ricky/memory-layer/scripts/mcp_stdio.py"],
      "env": { "MEMORY_USER_ID": "your_username" }
    }
  }
}
```

`scripts/mcp_stdio.py` is a thin wrapper that filters blank lines from Claude Desktop's Windows stdio before they reach FastMCP's strict JSON-RPC parser.

### npm bridge (hosted mode)

For users connecting to the hosted site without running Python locally:

```json
{
  "mcpServers": {
    "memoryLayer": {
      "command": "npx",
      "args": ["-y", "memory-layer"],
      "env": {
        "MEMORY_LAYER_URL": "https://yoursite.com/mcp/sse",
        "MEMORY_LAYER_TOKEN": "<api_token from /settings>"
      }
    }
  }
}
```

`npm/lib/bridge.js` — zero-dependency Node.js stdio MCP server that translates MCP tool calls into HTTP POSTs to `/mcp/sse`. No Python required on the client machine. Requires Node.js 18+.

### ChatGPT / Gemini / Grok

Use `POST /api/ingest` as a Custom GPT Action or function calling endpoint. An OpenAPI spec is planned for the settings page.

---

## Self-host / dev

```bash
git clone https://github.com/rickymm3/memory-layer.git
cd memory-layer
cp .env.example .env         # add DATABASE_URL, ANTHROPIC_API_KEY, CHAT_MODEL
docker compose up -d         # Postgres + pgvector
make doctor                  # verify full stack
make site                    # Synapse site on :5000
make mcp                     # MCP stdio server
make mcp-sse                 # MCP SSE server on :8765
make dashboard               # legacy dashboard on :5001
```

Key env vars:

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | — | `postgresql://user:pass@host:5432/dbname` |
| `ANTHROPIC_API_KEY` | — | Required for critic, reflect, and chat pipelines |
| `CHAT_MODEL` | `claude-haiku-4-5-20251001` | Must be an Anthropic model for JSON pipelines |
| `OLLAMA_HOST` | `http://localhost:11434` | Embedding + local LLM |
| `EMBEDDING_MODEL` | `qwen3-embedding:latest` | 4096-dim, local |
| `FLASK_SECRET_KEY` | — | Required for session auth (no dev fallback) |
| `MEMORY_USER_ID` | — | Default source identity in stdio MCP mode |

---

## What's planned

### Near-term
- Voyage AI embedding migration (`voyage-3`, 1024-dim, HNSW-compatible) before first real users
- Settings page config snippets for ChatGPT, Gemini, VS Code, local Ollama
- OpenAPI spec for ChatGPT Custom GPT Actions

### Core product: demand-driven question routing

When the AI's confidence on a question falls below threshold, instead of only falling back to web search, the system generates a **targeted forum-style post** directed at users whose profiles suggest they can answer — locals, experts, hobbyists, recent travelers, builders.

The loop:
```
User question → confidence check → AI-generated targeted post
→ relevant users notified → human responses
→ belief weighting (agreement/disagreement signals)
→ memory layer update → improved answer returned to original user
```

What gets stored per knowledge-gathering event:
- Original question + who asked
- Who was targeted and why (profile reasoning)
- What each respondent claimed
- Agreement/disagreement weight across responses
- Evidence quality signals
- Confidence score over time
- Whether the answer later proved useful (feedback loop)

The AI is not a passive consumer of human knowledge. It is an active questioner that routes gaps to the right humans. Normal social media: people post → AI consumes. This system: AI asks → people answer → AI stores → AI answers the next person who needs it.

### Embedding migration path
Current: `qwen3-embedding` (4096-dim, local Ollama). All 254 atoms use this model.
Target: Voyage AI `voyage-3` (1024-dim, HNSW-compatible) — enables proper indexing at scale and cross-user semantic search for federation. Migration deferred until pre-launch.

### Multi-platform MCP
- VS Code / Cursor / Continue.dev: MCP SSE URL (already works)
- Claude Desktop: WSL stdio (working)
- Browser extension: intercepts claude.ai, chatgpt.com sessions, injects memories, captures responses — no API key required

---

## Key design invariants

- Postgres rows are the source of truth. Embeddings are pointers.
- Signals are immutable. Atoms are revised, never deleted.
- All writes are dual: one `memory_atom` + one `memory_signal` per transaction.
- No silent writes. Every storage event is reported with both IDs.
- Parameterized SQL only. No f-string interpolation of user input.
- No direct HNSW index on `vector(4096)`. Exact cosine search only (until migration).
- `FLASK_SECRET_KEY` must be set. App raises `RuntimeError` if not.
- Critic's `suggested_visibility` forces private for: passwords, API keys, tokens, credentials, personal health/medical, home addresses, private financial details.
