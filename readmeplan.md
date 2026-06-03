# memory-layer: A Local LLM Memory System with MCP Integration

## What It Is

`memory-layer` is a local, single-user system that gives an LLM persistent, reconciled memory across sessions. Instead of starting every conversation from scratch, the system extracts facts, decisions, preferences, and instructions from your input, stores them durably in Postgres, and retrieves the most relevant ones as grounding context for every future response.

The memory-layer stack itself runs locally: Ollama, Postgres, pgvector, and the MCP server. When connected to cloud coding assistants such as GitHub Copilot, any memory returned through MCP may be included in that assistant's chat/tool context.

---

## The Core Problem It Solves

LLMs are stateless. Every chat session begins blank. If you've told your AI assistant "this project uses Postgres, not SQLite" fifty times, it doesn't remember the fifty-first time you open a new window.

`memory-layer` fixes that by maintaining a structured, queryable, deduplicated store of things worth remembering — and surfacing them automatically when relevant.

---

## Stack

| Component | Role |
|---|---|
| **Ollama** (`qwen3:8b`) | Local LLM — chat responses and reconciliation analysis |
| **Ollama** (`qwen3-embedding:latest`) | 4096-dimensional embeddings for semantic search |
| **PostgreSQL + pgvector** | Structured storage + cosine similarity search |
| **Python 3.12** | All scripts and server code |
| **psycopg3** | Postgres driver |
| **MCP SDK** (`mcp==1.27.1`) | Model Context Protocol server (stdio transport) |
| **Docker Compose** | Runs Postgres + pgvector locally |

---

## Two Core Data Structures

### `memory_atoms`
The source of truth. One row per distinct memory. Contains:
- `content` — a full, standalone sentence (e.g. "This project uses parameterized SQL only.")
- `context_summary` — a compact version for LLM prompts
- `memory_type` — `fact`, `decision`, `instruction`, `opinion`, `preference`, etc.
- `scope` — domain applicability (`project:<slug>`, `model:<name>`, `user`, global, or custom)
- `confidence`, `importance` — floats 0–1
- `embedding` — 4096-d vector for retrieval (semantic pointer, not truth)

### `memory_signals`
Immutable evidence records. Every confirmed or auto-stored write creates both an atom and a linked signal in a single transaction. Signals preserve provenance, source, and the full extraction context for future aggregation and weighting (Phase 3+).

---

## How Memory Gets In: The Write Flow

When you send a message during a chat session, the system runs this pipeline:

```
User message
    │
    ▼
LLM extracts candidate memories
    │
    ▼
Each candidate is embedded → pgvector finds related existing atoms
    │
    ▼
LLM reconciles: is this new, a duplicate, a conflict, an opinion change?
    │
    ▼
Write policy routes each candidate:
    ├── duplicate / reinforcement           → skip (no write, no report)
    ├── safe low-risk new / refinement      → auto-store + write report
    ├── risky new / refinement              → ask user: approve or reject
    ├── conflict / opinion_change           → ask user: approve or reject
    └── sensitive / personal / high-risk   → ask user: approve or reject
    │
    ▼
Exact-match guard (last-chance dedup)
    │
    ▼
INSERT memory_atom + INSERT linked memory_signal (single transaction)
```

**No silent writes.** Every storage event — auto or confirmed — reports the memory id, content, type, scope, and whether a linked signal was created.

---

## How Memory Gets Out: Retrieval

At the start of every chat turn, the system embeds your message and runs a cosine similarity search against `memory_atoms`. The top results are injected into the LLM prompt as grounding context before the response is generated.

Embeddings are semantic pointers to rows — the `content` column is the actual truth.

---

## Quick Start

```bash
cp .env.example .env
# edit .env: set DATABASE_URL, OLLAMA_HOST, CHAT_MODEL, EMBEDDING_MODEL

docker compose up -d        # start Postgres + pgvector
source .venv/bin/activate
make doctor                 # verify the full stack (expect 31 PASS, 1 WARN, 0 FAIL)
```

Start a live chat session with memory:

```bash
make session
```

Batch extract memories from a block of text:

```bash
make extract-store TEXT="your text here"
```

---

## CLI Reference

```bash
make session              # interactive chat with live memory retrieval + extraction
make extract-store        # batch extraction from a text block
make list                 # show recent memory atoms
make list-signals         # show recent memory signals
make list-task-runs       # list task_run records (Phase 7); --scope, --outcome, --id <uuid>
make model-report         # display model-scope atoms grouped by category; --model <name> [--all]
make assess-task ARGS="--scope project:<slug> --task '<desc>' [--model model:<name>] [--hint '<hint>'] [--web]"
                          # assess task readiness from stored memory; exit 0 = ready, 1 = not ready
make doctor               # environment health check
make mcp                  # start the MCP server manually (for testing outside VS Code)
make dashboard            # launch read-only Flask dashboard on port 5001
make reflect ARGS="..."   # end-of-task reflection (pass --store to write safe lessons)
make normalize-scope ARGS="--from <legacy> --to <canonical> [--yes]"
make install-vscode-prompts   # copy prompts/memory-layer-workflow.instructions.md to VS Code User prompts dir
                              # override: VSCODE_PROMPTS_DIR=~/.config/Code/User/prompts

# direct tools
.venv/bin/python scripts/update_memory.py <uuid> --content "..."
.venv/bin/python scripts/delete_memory.py <uuid>
.venv/bin/python scripts/retrieve_memory.py "query string"
.venv/bin/python scripts/test_mcp_handlers.py   # smoke test MCP handlers directly

# Phase 7: end-of-task reflection (dry-run by default; --store to write safe lessons)
.venv/bin/python scripts/reflect_task.py \
    --scope project:<slug> \
    --task "<short description>" \
    --files "<files changed>" \
    --tests "<test results>" \
    --outcome success|partial|failed \
    --notes "<observations and lessons>" \
    [--store]

# Scope normalization: rename a legacy scope to a canonical scope in memory_atoms
# (does not touch signals, does not regenerate embeddings)
.venv/bin/python scripts/normalize_scope.py \
    --from <legacy-scope> \
    --to <canonical-scope> \
    [--yes]
```

---

## MCP Server: Connecting to GitHub Copilot

The MCP server (`mcp_server/`) exposes the memory store to GitHub Copilot in VS Code via the
[Model Context Protocol](https://modelcontextprotocol.io/). VS Code launches it as a local
subprocess over stdio — no port, no daemon, no persistent process.

Configured in `.vscode/mcp.json`:
```json
{
  "servers": {
    "memoryLayer": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "envFile": "${workspaceFolder}/.env"
    }
  }
}
```

### Currently Live Read/Inspection Tools

| Tool | What it does |
|---|---|
| `memory_health` | Reports Postgres + Ollama reachability, atom count, and available scopes |
| `memory_search` | Semantic similarity search — primary Copilot retrieval tool; includes aggregate fields, `disagreement_flag`, and `signals_summary` |
| `memory_project_context` | High-importance/high-confidence atoms for a scope — orients Copilot at session start |
| `memory_recent` | Most recently stored atoms, newest first — browse without a query |
| `memory_get` | Fetch a single atom by UUID; includes aggregate fields, `disagreement_flag`, and `signals_summary` |
| `memory_task_context` | **Composite start-of-task tool** — project context + model lessons + recent task runs in one call; add `task_hint` for semantic search |
| `memory_assess_task_readiness` | **Readiness gate** — evaluates whether context is sufficient to proceed; returns `ready`, `confidence`, `recommended_action`, `reasons`, `risks`, `required_checks` |
| `memory_list_task_runs` | Recent task_run records — filterable by scope/outcome; review prior task history |
| `memory_extract_candidates` | Extract candidate atoms from text without storing |
| `memory_reconcile_candidate` | Classify a candidate against existing atoms without storing |
| `memory_get_signals` | Return signals linked to a memory atom — inspect provenance and disagreement evidence |

### Currently Live Write/Recompute Tools

| Tool | What it does |
|---|---|
| `memory_store_auto` | Auto-store a low-risk (new/refinement) candidate, return write report |
| `memory_propose_signal` | Queue a confirmation-required candidate for CLI review |
| `memory_store_approved` | Store after CLI issues a short-lived confirmation token |
| `memory_recompute_atom` | Recompute aggregation fields (support, opposition, confidence) for a single atom |

The MCP server **write tools route through the write policy** — Copilot cannot bypass it and cannot silently mutate memory.

---

## Bootstrap Workflow Instructions

### Why a bootstrap instruction, not only memory atoms

Memory atoms are retrieved by semantic similarity when relevant content is
already in the conversation. An atom that says "call `memory_project_context`
at session start" would only surface *after* the session has begun and
similar content has appeared — it cannot guarantee the agent loads project
context *before* the first task.

A `.instructions.md` bootstrap file is loaded unconditionally at the start of
every agent session. It configures **behavioral defaults**: when to call which
tool, how to evaluate what to store, how to classify scope, and how to report
writes. It is a standing operating procedure that runs before any task begins.

### What lives where

| Layer | What belongs there | Why |
|---|---|---|
| **Bootstrap instruction** (`prompts/memory-layer-workflow.instructions.md`) | *When* to call memory-layer tools, scope classification rules, write pipeline steps, reporting format | Needs to fire unconditionally at session start — cannot rely on semantic retrieval |
| **memoryLayer atoms** | Project decisions, constraints, facts, lessons, model observations | Retrieved by similarity when relevant; accumulates over time; survives across sessions and agent restarts |

Bootstrap instructions tell the agent **how to use** memoryLayer.
memoryLayer tells the agent **what to remember**.
They are complementary, not alternatives.

### Installing the workflow instructions

```bash
make install-vscode-prompts
```

This copies `prompts/memory-layer-workflow.instructions.md` to your VS Code
User prompts directory (default: `~/.vscode-server/data/User/prompts/`). Once
installed, VS Code Copilot loads it at the start of every agent session.

To override the install location:

```bash
make install-vscode-prompts VSCODE_PROMPTS_DIR=~/.config/Code/User/prompts
```

To customize for your workflow, edit the installed copy. To update the shared
template, edit `prompts/memory-layer-workflow.instructions.md` and re-run
`make install-vscode-prompts`.

### What the prompt teaches

- Call `memory_task_context` before planning any task (preferred: returns project context + model lessons + recent task runs in one call)
- Fall back to `memory_project_context`, `memory_search`, or `memory_list_task_runs` for targeted queries
- Evaluate user instructions and corrections for memory capture
- Classify scope as `project:<slug>`, `model:<name>`, `user`, or skip (current-chat-only)
- Store safe durable memories via the extract → reconcile → store pipeline
- Avoid storing temporary, vague, sensitive, conflicting, or identifying content
- Propose conflicts and opinion changes for CLI review — never auto-store them
- Report every write with `memory_atom_id` and `memory_signal_id`
- Summarize what was done and what comes next after each task

The prompt explicitly does **not** tell agents to store every instruction
blindly. It includes a "Do NOT capture" list and "when in doubt, skip" guidance.

---

## Key Invariants

- **Postgres rows are the source of truth.** Embeddings are pointers.
- **Signals are immutable.** Atom is written first; signal references it at creation — no post-creation mutation.
- **No silent writes.** Every storage event is reported.
- **Write policy, not universal gating.** Low-risk memories can auto-store; conflicts, opinion changes, sensitive claims, and high-risk instructions always require review.
- **No secrets in tool output.** Connection strings, file paths, and API keys never appear in MCP responses.
- **All writes are dual.** Every approved candidate stores a `memory_atom` + linked `memory_signal` in a single transaction.

---

## Current Phase Status

| Phase | Status | Description |
|---|---|---|
| 1 | ✅ Done | Memory atoms, extraction, reconciliation, CLI chat |
| 2 | ✅ Done | `memory_signals` table, dual atom+signal writes in one transaction |
| 2.5 | ✅ Done | MCP read-only server (`memory_health`, `memory_search`, `memory_project_context`, `memory_recent`), VS Code integration |
| 3 | ✅ Done | Write policy auto-store path, MCP write/proposal tools, full confirmation-path with CLI token review |
| 4 | ✅ Done | Signal aggregation: `support_weight`, `opposition_weight`, `disagreement_score`, recency + source decay, auto-recompute on write |
| 5 | ✅ Done | Disagreement escalation, signal inspection, source attribution, per-type recency half-lives, per-source trust weighting, task guardrails + project kickoff |
| 6 | ✅ Done | Local Memory Dashboard — read-only Flask UI, 5 routes, port 5001 |
| 6.5 | ✅ Done | Memory Lifecycle / Belief Revision — lifecycle_status, superseded_by, retrieval_priority; belief revision without deletion |
| 7 | ✅ Done | Model Adaptation Layer — task_runs schema, dashboard routes, MCP tools (memory_task_context, memory_list_task_runs), bootstrap prompt, model_report CLI, model_lessons dashboard |
| 7.6 | ✅ Done | Web Research Fallback — configurable provider abstraction, BraveSearchProvider, sensitive-content guard, no auto-store |
| 7.7 | ✅ Done | Confidence-Gated Agent Loop — deterministic readiness assessment, `memory_assess_task_readiness` MCP tool, CLI + 10-assertion test suite |
| 7.8 | ✅ Done | Epistemic-Status Handling — explicit supported/unsupported fact definitions in dashboard chat system prompts; model labels uncertain claims instead of suppressing them; opinions allowed; web-unavailability disclosed; verified by Test 14 in `test_web_research.py` |
| 8 | 🔮 Future Work | Moltbook-like Federated Memory |

---

## Phase 4 Completed Deliverables

### Signal aggregation
Implemented and verified. Every write auto-recomputes the owning atom. Fields on `memory_atoms`:
- `support_weight` — sum of agreeing signal weights (source decay: each repeated same-source signal counts half; recency half-life: 30 days)
- `opposition_weight` — sum of conflicting signal weights (same decay rules)
- `disagreement_score` — `opposition / (support + opposition)`, 0.0 if no scoreable signals
- `confidence` — `clamp(0.5 + 0.5×S/(S+1) − 0.5×O/(O+1), 0.1, 0.99)`
- `last_recomputed_at` — timestamp of most recent aggregation run

Deliverables: `app/signal_aggregator.py`, `MemoryStore.recompute_atom_weights()`, `memory_recompute_atom` MCP tool (11th tool), `scripts/recompute_weights.py` CLI (`make recompute-weights`), DB migration `003_add_signal_aggregation.sql`, doctor checks, 47-assertion test suite (`scripts/test_signal_aggregation.py`).

## What Comes Next (Phase 5 Priorities)

### Phase 5 Step 1: Disagreement escalation and signal inspection ✅ Complete

All read tools now expose aggregate fields. `disagreement_flag: true` is set when
`disagreement_score >= 0.5` — the threshold for treating an atom as "contested".

**Changes delivered:**
- `memory_search`, `memory_get`, `memory_recent`, `memory_project_context` all
  include: `support_weight`, `opposition_weight`, `disagreement_score`,
  `last_recomputed_at`, `disagreement_flag`
- New 12th MCP tool: `memory_get_signals` — returns the immutable signal evidence
  records linked to an atom, ordered newest first. Use this to understand *why*
  an atom has its current confidence and disagreement score.
- New CLI script: `scripts/show_memory.py <atom_id>` — prints atom + aggregate
  fields + signal summary with SUPPORT/CONFLICT classification per signal.
- `raw_input` is excluded from all MCP output.
- `make doctor`: 30 PASS, 1 WARN, 0 FAIL (after Step 3).

### Phase 5 Step 2: Source attribution in retrieval ✅ Complete

`memory_search` and `memory_get` now include a compact `signals_summary` dict in
every response:
```json
"signals_summary": {
  "count": 2,
  "top_sources": ["local_user"],
  "most_recent_signal_at": "2026-05-15T23:46:57+00:00"
}
```
Fields: `count` (total linked signals), `top_sources` (up to 3 distinct `source_key`
values ordered by most recent signal from each source), `most_recent_signal_at` (ISO
timestamp of newest signal, or null). No schema changes — signals are already linked
via FK. `fetch_signals_summary_batch()` helper in `mcp_server/tools/get_signals.py`
does the work in two parameterized queries over the open connection (no extra round-trip).
Atoms with no signals return `count=0`, `top_sources=[]`, `most_recent_signal_at=null`.
All Phase 5.2 handler tests pass.

### Phase 5 Step 3: Per-memory-type recency half-lives ✅ Complete

`RECENCY_HALF_LIFE_DAYS_BY_TYPE` dict added to `app/signal_aggregator.py`:

| `memory_type` | half-life |
|---|---|
| `instruction` | 180 days |
| `decision` | 90 days |
| `fact` | 90 days |
| `opinion` | 14 days |
| `preference` | 14 days |
| *(unknown / None)* | 30 days (fallback) |

`compute_atom_weights(signals, memory_type=None)` accepts the atom's type and
looks up the appropriate half-life. `recompute_atom_weights()` in `MemoryStore`
fetches the atom's `memory_type` and passes it through. The default 30-day half-life
is preserved for any type not in the table (backward-compatible).
`scripts/recompute_all_atoms.py` + `make recompute-all` bulk-recompute all stored
atoms with the new per-type values. 11 atoms recomputed on implementation.
New unit tests 1.13–1.15 added to `test_signal_aggregation.py` (all pass).
`make doctor`: 30 PASS, 1 WARN, 0 FAIL.

### Phase 5 Step 4: Per-source trust weighting / spam resistance ✅ Complete

The signal schema already has `source_key`. Phase 4 implements geometric source
decay (same-source repeat signals count half each time). Phase 5 Step 4 extends
this to global adversarial-source detection and trust-weighted scoring.

**What was added:**

- `compute_source_trust(source_stats)` — pure function in `app/signal_aggregator.py`.
  Reads global per-source stats (total signals, conflict count) and returns a
  `dict[str, float]` mapping adversarial source keys to `SOURCE_TRUST_FLOOR`.
- `compute_atom_weights(…, source_trust=…)` — new optional parameter; each
  signal's weight is multiplied by `source_trust.get(source_key, 1.0)`.
- `recompute_atom_weights` in `app/memory_store.py` now queries global source
  stats before calling `compute_atom_weights`, so trust is factored in on every
  recompute.
- No schema changes. Existing MCP tool interfaces unchanged.

**Spam detection thresholds (all in `app/signal_aggregator.py`):**

| Constant | Value | Meaning |
|---|---|---|
| `SPAM_MIN_SIGNAL_COUNT` | 5 | Minimum global signals before a source can be penalised |
| `SPAM_CONFLICT_RATIO_THRESHOLD` | 0.7 | Conflict ratio at or above which a source is adversarial |
| `SOURCE_TRUST_FLOOR` | 0.1 | Minimum trust multiplier (adversarial sources not silenced) |

**Tests added (Part 1 unit tests):**

- `1.16` — sparse source (< 5 signals) not penalised; low conflict ratio not penalised
- `1.17` — adversarial source gets `SOURCE_TRUST_FLOOR`; borderline (ratio == threshold) also penalised
- `1.18` — trust multiplier reduces opposition weight by expected ratio; `source_trust=None` is safe

All tests pass: `make doctor` still 30 PASS, 1 WARN, 0 FAIL.

### Phase 5 Step 5: Task guardrails / plan drift prevention ✅ Complete

**Scope convention (hard boundary):**

```
scope = "project:<slug>"
```

Project name is lowercased and non-alphanumeric characters become `-`.
Examples: `project:my-rails-app`, `project:node-service`, `project:memory-layer`.

Scope is the isolation boundary between projects.  `memory_project_context` and
`memory_search` filter by exact scope; embeddings are semantic pointers *inside*
that boundary — never the isolation mechanism.

**What was added:**

- `mcp_server/tools/kickoff.py` — `kickoff_project(project_name, discussion)`.
  Uses qwen3:8b with a specialised extraction prompt focused on tech stack
  decisions, architectural constraints, naming conventions, and "must not" rules.
  Stores each extracted item with `scope='project:<slug>'`, `importance=0.8`,
  `relationship='new'`.  Skips exact duplicates.  Returns
  `{scope, stored[], skipped_duplicates, count}`.
- Registered in `mcp_server/server.py` as `memory_project_kickoff` (13th tool).
- New integration test in `scripts/test_mcp_handlers.py` (Phase 5.5 block):
  Rails mock discussion → 9 atoms stored, all surfaced via `memory_project_context`,
  slug normalisation verified, error paths verified; cleanup deletes all test atoms.
- `~/.vscode-server/data/User/prompts/memory-layer-workflow.instructions.md` —
  user-level `applyTo: "**"` Copilot instructions covering:
  - Pre-task context retrieval: call `memory_project_context(scope='project:<slug>')` before writing code
  - Proactive decision storing: call `memory_store_auto` when a concrete decision is finalised
  - Kickoff usage: call `memory_project_kickoff` to batch-process a design discussion
  - Scope isolation rules and write policy summary

**Workflow:**

1. Start a new project → call `memory_project_kickoff(project_name, discussion)` to capture all decisions
2. Future sessions → Copilot calls `memory_project_context(scope='project:<slug>')` automatically before coding
3. During design chat → Copilot calls `memory_store_auto` when a decision is finalised
4. All writes still go through the existing write policy — `memory_store_auto` for safe items, `memory_propose_signal` for conflicts

---

## Phase 6: Local Memory Dashboard ✅ Complete

Read-only Flask web UI for browsing the memory layer. Launch with `make dashboard`, opens on port 5001.

### Files created

| Path | Purpose |
|------|---------|
| `dashboard/__init__.py` | Package marker |
| `dashboard/app.py` | Flask app — routes, direct psycopg for read views, MemoryStore + write-policy for chat |
| `dashboard/templates/base.html` | Pico CSS CDN nav shell |
| `dashboard/templates/atoms.html` | Atom list with scope/type/confidence filters |
| `dashboard/templates/atom_detail.html` | Full atom card + signal provenance table |
| `dashboard/templates/contested.html` | Pre-filtered: disagreement_score ≥ 0.5 |
| `dashboard/templates/proposals.html` | Pending proposals queue (read-only) |
| `dashboard/templates/scopes.html` | Per-scope summary — atom count, contested, low-confidence, avg confidence, newest |
| `dashboard/templates/chat.html` | Chat UI — retrieves memories, displays write events per turn |
| `dashboard/templates/404.html` | Custom 404 page |

### Routes

| Route | Description |
|-------|-------------|
| `GET /` | Atom list; filter by `?scope=&type=&min_confidence=`; max 100 rows |
| `GET /scopes` | Per-scope summary: total atoms, contested count, low-confidence count, avg confidence, newest memory, links to `/?scope=<slug>` filtered list |
| `GET /atom/<uuid>` | Full atom card + signal provenance timeline; 404 for unknown UUID |
| `GET /contested` | Atoms with `disagreement_score >= 0.5`, sorted DESC |
| `GET /proposals` | `status='pending_review'` proposals (approve via `make review-proposals`) |
| `GET /chat` | Chat UI — retrieves memories as context, displays write events per turn |
| `POST /chat` | Submit a message: retrieve memories → LLM response → extract + store/propose |
| `POST /chat/clear` | Clear the in-session conversation history |

### Architecture decisions

- Framework: Flask 3.1.3, Jinja2 templating
- CSS: Pico CSS v2 via CDN — no JS framework, semantic HTML only
- DB access: read-only routes use `psycopg.connect(get_config().database_url)` directly; `/chat` uses `MemoryStore` for retrieval and the write-policy path for extraction/storage
- All SQL parameterised (no f-string interpolation of user input)
- Flask `--no-debugger` flag required (added in Makefile target)
- `uuid:` route converter provides automatic 400/404 on malformed UUIDs
- Port 5001 — avoids clash with MCP server stdio transport
- `/chat` write policy mirrors MCP: `new`/`refinement` auto-stored, `conflict`/`opinion_change` queued to `memory_proposals`, `duplicate`/`reinforcement` skipped
- Chat conversation history stored in Flask session cookie (trimmed to 20 messages / 10 turns)

### Dashboard / MCP parity

Dashboard chat and Copilot MCP both read from and write to the same `memory_atoms` / `memory_signals` tables in Postgres.

| Direction | Path | Verified |
|---|---|---|
| MCP → Dashboard | `store_memory_auto` → `MemoryStore.retrieve_memories` | ✓ |
| Dashboard → MCP | `extract_and_store_from_message` → `search_memories` | ✓ |

Parity test: `scripts/test_chat_parity.py` (`make test-chat-parity`).

### Makefile target

```makefile
dashboard:
    FLASK_APP=dashboard.app .venv/bin/flask run --port 5001 --no-debugger
```

### Test results

All routes validated with `curl` against live server:

```
200  /
200  /scopes
200  /contested
200  /proposals
200  /atom/a439d8d0-5238-4d9b-ab29-ab266de36b2e   (existing atom)
404  /atom/00000000-0000-0000-0000-000000000000   (unknown UUID)
```

---

**What this is not:**

- Does **not** replace `readmeplan.md`. The plan remains the source of truth.
- Does **not** use `.github/copilot-instructions.md` for dynamic task memory.
  The instructions file at `~/.vscode-server/data/User/prompts/` is user-level
  and static; dynamic per-project context lives in memoryLayer where it decays,
  can conflict, and is subject to the write policy.


---

## Phase 6.5: Memory Lifecycle / Belief Revision ✅ Complete

Adds lifecycle tracking to `memory_atoms` so the system can **preserve old beliefs while
promoting newer, more accurate ones** — without deleting any data.

### The core problem it solves

`confidence` measures correctness probability; `disagreement_flag` surfaces evidence-based
conflict. Neither handles temporal revision: "I used to believe X, but Y replaced it." Without
lifecycle tracking, superseded memories compete equally with current ones in retrieval.

### Lifecycle statuses

| Status | Retrieval behaviour | Notes |
|---|---|---|
| `active` | Full retrieval (default) | Normal state for all atoms |
| `superseded` | Excluded from `memory_project_context`; present in `memory_search` | Belief replaced by a newer atom |
| `deprecated` | Excluded from `memory_project_context`; present in `memory_search` | Manually flagged as no longer relevant |
| `archived` | Excluded from `memory_project_context`, `memory_search`, and `memory_recent` | Historical record only |
| `contested` | Excluded from `memory_project_context` | Administratively flagged; distinct from signal-computed `disagreement_flag` |

### New schema columns (migration `004_add_lifecycle.sql`)

```sql
lifecycle_status       TEXT NOT NULL DEFAULT 'active'
superseded_by_atom_id  UUID REFERENCES memory_atoms(id) ON DELETE SET NULL
lifecycle_reason       TEXT
retrieval_priority     FLOAT NOT NULL DEFAULT 1.0
lifecycle_updated_at   TIMESTAMPTZ
```

`retrieval_priority` separates "surfacing frequency" from `confidence` (correctness probability).
All existing rows default to `active` / `retrieval_priority=1.0` — fully backward-compatible.

### Key invariants

- **No data deleted.** Atoms and signals are never removed by lifecycle transitions.
- **Signals remain immutable.** `confidence`, `support_weight`, `disagreement_score` are not changed.
- **`memory_get` always works.** Any atom — including `archived` — is inspectable by UUID.
- **Write policy unchanged.** New atoms always start as `active`.

### New CLI tool

```bash
# Supersede old belief, point to replacement:
.venv/bin/python scripts/supersede_memory.py <old_atom_id> <new_atom_id> \
    --reason "Write policy refined to risk-based approach."

# Deprecate without a replacement:
.venv/bin/python scripts/supersede_memory.py <old_atom_id> \
    --status deprecated --reason "No longer relevant."

# Archive (hidden from all retrieval):
.venv/bin/python scripts/supersede_memory.py <old_atom_id> \
    --status archived --reason "Historical record only."
```

### Dashboard changes

- Atom list: **Status** column with colour-coded badges; superseded/deprecated/archived rows dimmed.
- Atom detail: Lifecycle banner shows status, reason, superseded-by link, and `lifecycle_updated_at`.
- Retrieval priority shown alongside confidence on the detail page.

---

## Phase 7: Model Adaptation Layer ✅ Done

> **This is external learning, not weight training.** The model itself remains static. The
> memory/control layer improves its behaviour by retrieving persistent experience, corrections,
> successful patterns, project knowledge, and task guardrails — making a weaker, local, or
> cheaper model behave as though it is improving over time.

### The core idea

A weaker model alone forgets, repeats mistakes, lacks project context, and does not know what
worked last time. A weaker model **+ memory-layer**:

- retrieves project context before planning
- sees prior corrections and avoids known bad decisions
- checks task guardrails before acting
- uses known-good patterns from prior task runs
- stores lessons from completed work
- gets more reliable at a specific user/project over time

This is not traditional training. It is an **externalized learning loop** — the system observes
outcomes, stores lessons, updates confidence, detects repeated errors, and changes future
prompt/tool behaviour. The model does not rewrite its weights; the memory-layer accumulates
the experience the model cannot.

### Planned components

#### 1. `model_profiles`
Per-model durable records:
- model name, provider, and local identifier
- known strengths
- known weaknesses (e.g. "forgets to run tests", "over-edits unrelated files")
- preferred prompting style
- reliability notes

#### 2. `task_runs`
One record per completed task:
- task description and project scope
- model used
- outcome: `success` / `fail` / `partial`
- tests run
- errors encountered
- user corrections applied

#### 3. `model_lessons`
Lessons distilled from prior model usage, scoped to a model, project, task type, or global:
- "This model often forgets to run tests."
- "This model over-edits unrelated files."
- "This model needs explicit Rails migration reminders."

#### 4. `prompt_adaptations`
Dynamic prompt/guardrail generation based on:
- active model
- active project
- task type
- prior failures and corrections
- retrieved project memory

Example model profile compensation:
```
model: small-coder-7b
known weaknesses:
  - forgets tests
  - over-edits unrelated files
  - weak at Rails conventions

memory-layer compensations:
  - always retrieve Rails project conventions before planning
  - inject "do not edit unrelated files" guardrail
  - require test checklist before marking task complete
```

#### 5. Eval harness
- Compare model performance with and without memory-layer adaptation.
- Track whether the model completes tasks more reliably over time.
- Measure correction rate, test-pass rate, and task outcome distribution.

### First milestone (manual / no new tables)

Before adding new schema, validate the concept with existing infrastructure:

- Track model weaknesses and user corrections manually as `memory_atoms` (scope: model identifier, type: `instruction` or `preference`).
- Use existing `memory_signals` for provenance and confidence weighting.
- Begin with **one local model** and **one coding workflow** only.
- Do **not** implement fine-tuning or weight modification.
- Do **not** start with multi-model orchestration.
- Promote to dedicated tables (`model_profiles`, `task_runs`, `model_lessons`) only after the
  manual phase validates which data is actually useful.

#### End-of-task reflection workflow

`scripts/reflect_task.py` is the accumulation mechanism for the manual-first milestone.
Run it after any implementation task to extract concrete lessons and optionally store safe
ones via the existing write policy.

```bash
# Dry-run (default) — shows candidates, writes nothing:
python scripts/reflect_task.py \
    --scope project:memory-layer \
    --task "Added /scopes dashboard route" \
    --files "dashboard/app.py dashboard/templates/scopes.html" \
    --tests "make doctor: 30 PASS, 1 WARN, 0 FAIL; 6-route smoke test: all 200/404" \
    --outcome success \
    --notes "Parameterized SQL only. NULL scopes displayed as (none)."

# Store safe lessons via write policy:
python scripts/reflect_task.py ... --store
```

Behaviour:
1. Prints a structured reflection header (scope, task, files, tests, outcome, notes).
2. Calls the LLM to extract concrete, reusable candidate lessons from the task context.
3. Classifies each candidate: `safe_to_store` | `needs_review` | `skip`.
4. In dry-run mode: prints all candidates with classification — no writes.
5. In `--store` mode: calls `store_memory_auto` for `safe_to_store` items only.
6. Every stored lesson creates one `memory_atom` + one linked `memory_signal` in a single
   transaction. The write report prints both IDs.
7. Never writes silently — every write is reported.

### What this phase does not do

- Does not modify model weights.
- Does not require a new LLM provider.
- Does not replace the existing write policy — all lessons still go through extraction, reconciliation, and the write policy gate.

---

### Phase 7 Step 1: `task_runs` schema ✅ Complete

**The gap it closes:** the flat `memory_atoms` model could not answer "which tasks used a given model and what were their outcomes?" Each stored lesson had `reconciliation_reason=task_reflection` but no link to a structured task-run record.

**Deliverables:**

| File | Change |
|------|--------|
| `db/migrations/005_add_task_runs.sql` | New `task_runs` table + `task_run_id UUID FK` on `memory_signals` |
| `db/init.sql` | Same DDL appended for fresh installs |
| `app/memory_store.py` | `task_run_id: str \| None = None` param in `store_memory_with_signal`; threaded into signal INSERT |
| `mcp_server/tools/store_auto.py` | `task_run_id` param passed through to `store_memory_with_signal` |
| `scripts/reflect_task.py` | `_create_task_run()` + `_update_task_run_lesson_count()` helpers; store mode creates task_run first, links every signal, updates `lessons_stored` after |
| `scripts/check_environment.py` | `task_run_id` in `EXPECTED_SIGNAL_COLUMNS` + `EXPECTED_SIGNAL_INDEXES`; new `task_runs exists` check |

**`task_runs` schema:**
```sql
id, scope, task_description, model_used, files_changed,
test_results, outcome CHECK('success','partial','failed'),
notes, lessons_stored, created_at
```

**Key design decision:** FK is on `memory_signals`, not `memory_atoms`. Signals are immutable provenance records tied to the write event; an atom can be reinforced by signals from multiple future task runs.

**Verification:** `make doctor`: 31 PASS, 1 WARN, 0 FAIL. FK smoke test: `task_run_id` correctly propagated from `reflect_task.py --store` through `store_memory_auto` → `store_memory_with_signal` → signal INSERT.

---

### Phase 7 Step 2: `task_runs` dashboard route ✅ Complete

Adds task run history to the Flask dashboard so task provenance is browsable in the web UI without CLI tools.

**New routes:**

| Route | Description |
|-------|-------------|
| `GET /task_runs` | List all task_runs, newest first; filter by `?scope=&outcome=`; max 100 rows |
| `GET /task_run/<uuid>` | Full task_run card (scope, task, model, outcome, files, tests, notes) + linked lessons table; 404 for unknown UUID |

**Files changed:**

| File | Change |
|------|--------|
| `dashboard/app.py` | `task_runs()` and `task_run_detail()` route handlers |
| `dashboard/templates/task_runs.html` | List view with scope/outcome filter form and outcome badges |
| `dashboard/templates/task_run_detail.html` | Detail card + linked lessons table with atom links |
| `dashboard/templates/base.html` | "Task Runs" nav link added |

**Verification:** `200 GET /task_runs`, `200 GET /task_run/<existing-uuid>`, `404 GET /task_run/<zero-uuid>`.

---

### Phase 7 Step 3: `memory_list_task_runs` MCP tool ✅ Complete

Adds `memory_list_task_runs` to the MCP server so Copilot can query task run history before starting new work, completing the situational-awareness loop alongside `memory_project_context` and `memory_recent`.

**Files changed:**

| File | Change |
|------|--------|
| `mcp_server/tools/list_task_runs.py` | New handler: `list_task_runs(scope, outcome, limit)` — parameterized SQL, outcome validated against `VALID_OUTCOMES` before use |
| `mcp_server/server.py` | Import + `@mcp.tool()` registration of `memory_list_task_runs` |

**Tool signature:**
```
memory_list_task_runs(scope?, outcome?, limit=10)
```
Returns: `id, scope, task_description, model_used, files_changed, outcome, lessons_stored, created_at` (ISO 8601). Limit clamped 1–50.

**Verification:** 14 tools registered (was 13); functional test returns 2 task_runs for `project:memory-layer` scope.

> **Note:** MCP server restart required in VS Code for the new tool to be recognized.

---

### Phase 7 Step 4: `memory_task_context` composite MCP tool ✅ Complete

Closes the **start-of-task** half of the Phase 7 adaptation loop. Previously, orienting Copilot for a new task required three separate tool calls: `memory_project_context` + `memory_recent` (model scope) + `memory_list_task_runs`. This collapses them into one.

**Files changed:**

| File | Change |
|------|--------|
| `mcp_server/tools/task_context.py` | New handler — single psycopg connection runs 3 SQL queries; optional fourth section (semantic search) only when `task_hint` provided |
| `mcp_server/server.py` | Import + `@mcp.tool()` registration of `memory_task_context` |

**Tool signature:**
```
memory_task_context(project_scope, model_scope?, task_hint?, recent_tasks=5)
```

**Response sections:**

| Section | Content | Condition |
|---|---|---|
| `project_context` | High-importance project atoms (importance ≥ 0.5, confidence ≥ 0.6) | Always |
| `model_lessons` | All active atoms from `model_scope` — weaknesses + prompt adaptations | When `model_scope` provided |
| `recent_task_runs` | Last N task_run records for `project_scope` | Always |
| `task_relevant_atoms` | Semantic matches for `task_hint` (similarity ≥ 0.45) | When `task_hint` provided; adds ~500 ms for embedding |
| `summary` | Counts + `task_run_outcomes` dict | Always |

**Verification:** 15 tools registered (was 14); functional test: 15 project atoms, 6 model lessons, 3 task runs for `project:memory-layer` + `model:qwen3-8b`. `make doctor`: 31 PASS, 1 WARN, 0 FAIL.

> **Note:** MCP server restart required in VS Code for the new tool to be recognized.

---

### Phase 7 Step 5: Bootstrap prompt updated to use `memory_task_context` ✅ Complete

Updated `prompts/memory-layer-workflow.instructions.md` Session start section to use
`memory_task_context(project_scope, model_scope, task_hint)` as the primary tool,
demoting individual tools (`memory_project_context`, `memory_search`,
`memory_list_task_runs`) to the "Alternatives for specific use cases" section.
Updated readmeplan "What the prompt teaches" list to match. Re-installed via
`make install-vscode-prompts`. `make doctor`: 31 PASS, 1 WARN, 0 FAIL.

---

### Phase 7 Step 6: `model_report.py` CLI ✅ Complete

Adds a structured model audit CLI that displays model-scope atoms grouped by category
without requiring SQL or the dashboard.

**Command:**
```bash
make model-report                          # all model:* scopes
make model-report ARGS="--model qwen3-8b"  # specific model
make model-report ARGS="--model qwen3-8b --all"  # include non-active atoms
```

**Files changed:**

| File | Change |
|------|--------|
| `scripts/model_report.py` | New script — direct psycopg, groups by `memory_type` into Model Observations / Prompt Adaptations / General |
| `Makefile` | `model-report` target added |

**Category mapping:**

| `memory_type` | Display section |
|---|---|
| `fact`, `decision` | Model Observations |
| `instruction` | Prompt Adaptations |
| anything else | General |

**Verification:** 6 active atoms for `model:qwen3-8b` — 2 Model Observations, 4 Prompt
Adaptations. `--all` shows 8 atoms with `[superseded]` / `[deprecated]` status flags.
`make doctor`: 31 PASS, 1 WARN, 0 FAIL. No new DB schema required.

---

### Phase 7 Step 7: `/model_lessons` dashboard route ✅ Complete

Adds model lessons view to the Flask dashboard so model-scope atoms are browsable
in the web UI with the same category grouping as the CLI.

**New route:**

| Route | Description |
|-------|-------------|
| `GET /model_lessons` | Model-scope atoms grouped by Model Observations / Prompt Adaptations / General; filter by `?model=<name>` and `?include_inactive=1` |

**Files changed:**

| File | Change |
|------|--------|
| `dashboard/app.py` | `model_lessons()` route handler; same `memory_type` category mapping as CLI |
| `dashboard/templates/model_lessons.html` | Grouped view with model/include_inactive filter form, per-category tables with conf/imp/lifecycle columns |
| `dashboard/templates/base.html` | "Model Lessons" nav link added (between Task Runs and Chat) |

**Verification:** `200 GET /model_lessons`, `200 GET /model_lessons?model=qwen3-8b`.
`make doctor`: 31 PASS, 1 WARN, 0 FAIL. No new DB schema.

**Phase 7 complete.** All 7 steps delivered: task_runs schema → dashboard task routes →
MCP list tool → composite memory_task_context → bootstrap prompt → model_report CLI →
model_lessons dashboard.

---

#### Reflect runs completed

| Run | Scope | Lessons stored | Notes |
|---|---|---|---|
| 1 | `project:memory-layer` | 5 | Scope normalization task |
| 2 | `project:memory-layer` | 8 | Phase 6.5 lifecycle/belief revision |
| 3 | `model:qwen3-8b` | 6 | Phases 1–6.5 behavioral observations |
| 4 | `project:memory-layer` | 5 | reflect_task.py near-duplicate fix (4 auto + 1 manual) |
| 5 | `project:memory-layer` | 1 | MCP subprocess staleness — restart required after mcp_server/ changes (atom `e944ab6f`) |
| 6 | `project:memory-layer` | 3 | Phase 7 Step 1: task_runs schema + reflect_task.py integration (task_run `c9390bac`) |
| 7 | `project:memory-layer` | 3 | Phase 7 Step 2: task_runs dashboard route (task_run `6ab7291f`) |
| 8 | `project:memory-layer` | 3 | Phase 7 Step 3: memory_list_task_runs MCP tool (task_run `afc950e4`) |
| 9 | `project:memory-layer` | 3 | Bootstrap workflow instructions: prompts/ + make install-vscode-prompts (task_run `65883ff0`) |
| 10 | `project:memory-layer` | 3 | Phase 7 Step 4: memory_task_context composite MCP tool (task_run `6f813676`) |
| 11 | `project:memory-layer` | 1 | Phase 7 Step 5: bootstrap prompt updated to use memory_task_context (task_run `70b7f530`) |
| 12 | `project:memory-layer` | 5 | Phase 7 Step 6: model_report.py CLI — grouped model-scope atom view (task_run `40f70e00`) |
| 13 | `project:memory-layer` | 3 | Phase 7 Step 7: /model_lessons dashboard route — Phase 7 complete (task_run `cd88f369`) |

#### model:qwen3-8b audit (after reflect run 3)

6 active lessons, 2 non-active (preserved via lifecycle):

| Atom | Type | Category | Status |
|---|---|---|---|
| "qwen3:8b should be given explicit test requirements before coding tasks…" | instruction | model weakness | active (primary) |
| "…generates valid parameterized SQL but requires explicit no-f-string reminders." | instruction | prompt adaptation | active |
| "…maintains reconciliation accuracy with clear schemas but drifts on open-ended tasks." | fact | model weakness | active |
| "…requires structured prompts with numbered steps…for multi-file changes." | instruction | prompt adaptation | active |
| "…benefits from explicit examples in prompts for implicit convention extraction." | instruction | prompt adaptation | active |
| "…mathematical formula implementations may require multiple correction iterations…" | fact | model weakness | active |
| "For model:qwen3-8b, explicit test requirements are necessary…" | instruction | near-duplicate | **deprecated** |
| "Signal aggregation formulas for model:qwen3-8b require multiple correction iterations…" | fact | project-specific wording | **superseded** |

#### Findings

**Finding 1: near-duplicate detection gap in `reflect_task.py`**

The reflect LLM extraction step had no awareness of what was already stored in the target
scope. A near-duplicate of the pre-existing test-requirements lesson passed through as `new`
because the reconciler's cosine similarity was not tight enough to trigger dedup at 0.93
threshold.

**Fix applied:** `reflect_task.py` now fetches all active atoms in the target scope before
calling the LLM and injects them as an "Existing lessons" context block in the extraction
prompt. The LLM is instructed to classify any candidate that overlaps with an existing lesson
as `skip`. Verified: same near-duplicate content now classifies as `skip`.

**Finding 2: flat `memory_atoms` model works for model lessons**

The 6 active `model:qwen3-8b` atoms naturally cluster into two shapes:
- **Model weaknesses** (3 atoms): observations about what the model gets wrong
- **Prompt adaptations** (3 atoms): instructions about what the model needs to perform correctly

This maps cleanly to `model_profiles.known_weaknesses` and `prompt_adaptations` in the Phase 7
schema plan. The flat model is sufficient for storage and retrieval. No dedicated tables are
warranted yet.

**Gap identified: no task-run provenance**

The flat model cannot answer "which tasks used model:qwen3-8b and what were their outcomes?"
Each lesson is an atom with `reconciliation_reason=task_reflection` but no link to a structured
task-run record. This is the strongest signal that `task_runs` is the one Phase 7 table with
genuine value that the flat model cannot replicate.

**Decision: continue accumulating before adding schema.** Target: 3+ more reflect runs (mix
of `project:` and `model:` scopes) before designing the `task_runs` table.

**Finding 3: MCP server subprocess staleness**

The VS Code MCP server subprocess is started once per VS Code session and caches the loaded
code. If MCP tool code is updated after the server starts, the running subprocess will execute
old code until VS Code restarts it. Symptom: `memory_project_context` returned deprecated and
superseded atoms (lifecycle fields absent from response, `AND lifecycle_status = 'active'`
filter missing from query). Fix: restart the MCP server via VS Code command palette after any
code change to `mcp_server/`.

**Finding 4: proof-of-value confirmed via MCP after server restart**

`memory_project_context(scope="model:qwen3-8b", min_importance=0.5, min_confidence=0.5)` returned
exactly 6 atoms, all `lifecycle_status: active`. Zero deprecated or superseded atoms leaked
through. All 6 context_summaries are actionable pre-task guidance for a qwen3:8b coding
session. The flat model + lifecycle filtering is sufficient for model adaptation without new
schema.

---

## Phase 7.6: Web Research Fallback ✅ Done

Adds a configurable web research fallback to the `/chat` dashboard route.
When enabled and a suitable provider is configured, the assistant can supplement
its response with live web results — while keeping the existing memory write policy
intact.

### Design goals

- **Opt-in by default** — `WEB_RESEARCH_ENABLED=false`; the app boots and runs
  normally with no search provider configured.
- **Configurable provider** — `WEB_SEARCH_PROVIDER` selects the backend.
  Currently: `none` (no-op) and `brave` (Brave Search API). The provider
  interface (`BaseResearchProvider`) makes adding new backends straightforward.
- **No silent memory writes** — raw web results are never auto-stored as memory
  atoms. Any durable lesson discovered via research still goes through the
  existing `extract → reconcile → store_memory_auto / propose_memory_signal` pipeline.
- **No schema changes** — no new DB tables or columns.
- **Sensitive-content guard** — messages containing patterns like `password`,
  `api_key`, `token`, `database_url`, etc. are never sent to external providers.

### Trigger logic

Research is attempted when **all** of:
1. Provider is available (`WEB_RESEARCH_ENABLED=true` + valid API key)
2. Message does not contain sensitive patterns
3. User has not said "no search" / "local only" / "don't search"

**And at least one of:**
- Message contains an explicit keyword (`search`, `latest`, `current`, `look up`, `docs for`, …)
- Local memory retrieval returned no results above the 0.45 similarity threshold

### Guardrails

| Guardrail | Implementation |
|---|---|
| No auto-store of web results | `provider.search()` returns dicts; write pipeline never called on them |
| Sensitive content blocked | `_SENSITIVE_RE` pattern check before any provider call |
| Graceful disable | `NoOpProvider` always returns `[]`; `/chat` works unchanged |
| Provider interface | `BaseResearchProvider` ABC — swap providers without touching Flask route |
| UI transparency | "🌐 N web results used" or "🌐 Web research requested — no provider configured" shown per message |
| No schema changes | Confirmed: `make doctor` 0 FAIL |

### New files

| File | Purpose |
|---|---|
| `app/research.py` | Provider abstraction, `NoOpProvider`, `BraveSearchProvider`, `should_use_research()`, `get_research_provider()` |
| `scripts/test_web_research.py` | 12-assertion test suite (`make test-web-research`) |

### Modified files

| File | Change |
|---|---|
| `app/config.py` | `web_research_enabled`, `web_search_provider`, `web_search_api_key` fields |
| `app/chat.py` | `build_prompt_with_history(research_results=...)`, new `chat_with_research()` |
| `dashboard/app.py` | `/chat` POST calls `chat_with_research()`; stores `research_status`, `research_count` |
| `dashboard/templates/chat.html` | Research badge CSS + Jinja rendering |
| `.env.example` | Web research config section |
| `scripts/check_environment.py` | `web research config` PASS/WARN check |
| `Makefile` | `test-web-research` target |

### Config keys

```env
WEB_RESEARCH_ENABLED=false        # master switch; default off
WEB_SEARCH_PROVIDER=none          # none | brave  (more planned)
WEB_SEARCH_API_KEY=               # required when provider != none
```

### Verification

```
make test-web-research   # 12 PASS, 0 FAIL
make test-chat-parity    # 5 PASS, 0 FAIL (no regression)
make doctor              # 32 PASS, 1 WARN, 0 FAIL
```

---

## Phase 7.7: Confidence-Gated Agent Loop ✅ Done

Adds a self-monitoring readiness layer that evaluates whether the agent has
sufficient context to act before touching any files. All rules are deterministic
and inspectable — no LLM inference in the readiness gate.

### The core problem it solves

`memory_task_context` retrieves context, but the agent previously proceeded
regardless of context quality. Thin context, contested atoms, missing tests, or
tasks requiring current external information all produce silent blind spots.
Phase 7.7 makes those gaps explicit before the first edit.

### `app/task_readiness.py`

Pure helper function — no DB queries, no network calls. Takes a `task_context`
dict (from `memory_task_context` output) and applies seven deterministic rules:

| Rule | Trigger | Penalty | Action |
|---|---|---|---|
| 1 | No project context | −0.50 | `project_kickoff` |
| 2 | Task hint given, no relevant atoms | −0.20 | `retrieve_more` |
| 3 | Contested/non-active atom in context | −0.25 | `inspect_conflict` |
| 4 | "current", "latest", "API", "docs" keywords | −0.15 | `search_web` |
| 5 | Coding task + no test spec | −0.15 | `define_tests` |
| 6 | Model lesson requires explicit tests | −0.10 | `define_tests` |
| 7 | Thin context (< 3 atoms, no hint) | −0.10 | `retrieve_more` |

Confidence = `1.0 − sum(penalties)`, clamped `[0, 1]`. `ready=False` when
confidence < 0.60 **or** when `project_kickoff`/`inspect_conflict` is triggered
(hard blockers regardless of score).

### `mcp_server/tools/assess_readiness.py` + MCP tool `memory_assess_task_readiness`

Thin wrapper: calls `get_task_context` (single DB round-trip) then
`assess_task_readiness`. Returns the full verdict plus `task_context_summary`
for diagnostic use.

### `scripts/assess_task.py` + `make assess-task`

CLI tool: exits 0 if ready, 1 if not ready. Prints JSON verdict.

```bash
make assess-task ARGS="--scope project:memory-layer \
    --task 'implement OAuth login' \
    --model model:qwen3-8b \
    --hint 'auth login OAuth'"
```

### `scripts/test_task_readiness.py` + `make test-task-readiness`

10-assertion test suite covering all rules:
1. Empty project context → `project_kickoff`, not ready
2. "latest"/"API" keyword → `search_web`
3. Coding task without test spec → `define_tests`
4. Contested atom (`disagreement_flag=True`) → `inspect_conflict`, not ready
5. Sufficient context, non-coding task → `proceed`, ready
6. Model lesson with test requirement → `required_checks` populated
7. Task hint given, no relevant atoms → `retrieve_more`
8. Read-only task → no `define_tests`
9. `user_requested_web=True` → `search_web`
10. Deprecated atom in context → `inspect_conflict`, not ready

### `mcp_server/tools/task_context.py` — updated

All three SELECT queries now include `disagreement_flag`. The `project_context`
query also exposes `lifecycle_status` in the dict. No schema changes — these
fields already existed.

### Bootstrap prompt — updated

`prompts/memory-layer-workflow.instructions.md` now includes a **Readiness
assessment** subsection after the Session start block, with a full
`recommended_action` reference table and instructions not to proceed if
`ready=false`.

### Guardrails respected

- No memory write policy changes
- No signal aggregation formula changes
- No new DB schema (existing `disagreement_flag` column now surfaced in output)
- No federation / Moltbook implementation
- Web results never auto-stored

### Verification

```
make test-task-readiness   # 10 PASS, 0 FAIL
make test-web-research     # 12 PASS, 0 FAIL (no regression)
make test-chat-parity      # 5 PASS, 0 FAIL (no regression)
make doctor                # 34 PASS, 1 WARN, 0 FAIL
```

---

## Phase 8: Moltbook-like Federated Memory 🔮 Future Work — Not Yet Implemented

> **Status:** Design concept only. No code, no schema, no networking. This phase
> does not begin until local memory, task_runs, model adaptation, web research,
> and dashboard review are fully stable.

Independent `memory-layer` instances are local **brains**. Each brain owns its
own memory and runs offline-first. Brains can optionally connect peer-to-peer
or through a federation layer to exchange selected evidence and claims — but
they never share a global database and peers never write directly into another
brain's memory.

### Core model

- Each local brain is a self-contained `memory-layer` instance (Postgres + pgvector).
- Brains exchange **evidence**, not database rows. A peer's claim enters the
  local write pipeline as an external signal, not a direct INSERT.
- Every candidate from a peer goes through the same
  `extract → reconcile → store_memory_auto / propose_memory_signal` write policy
  that governs local and MCP writes.
- Old beliefs are preserved and evolved using the existing lifecycle/belief-revision
  mechanism (active → superseded/deprecated/archived). Peer data never silently
  overwrites a local atom.
- Human users remain in control through the dashboard review queue.

### Core principles

#### 1. Local authority
- A local brain owns its own memory. It is the sole authority over its atoms.
- Peer data arrives as external signals. The local brain decides whether to
  reinforce, contest, ignore, supersede, or store a candidate belief.
- No remote peer may directly INSERT, UPDATE, or DELETE a local atom.

#### 2. Separate layers: evidence, belief, and belief history
- **Evidence** — local observations, MCP writes, web research snippets, or
  incoming peer signals.
- **Current belief** — the active `memory_atom` with its current confidence,
  importance, and lifecycle status.
- **Belief history** — superseded, deprecated, and archived atoms, each with
  a `lifecycle_reason` recording why the belief changed.

#### 3. Public brain trust / reputation
- Each peer brain may carry a trust score, stored locally.
- Trust should be **topic-specific**, not only global: a brain that is reliable
  about Python packaging may be unreliable about hardware specifications.
- Reliable brains gain influence over time; repeatedly wrong or spammy brains
  lose it. Trust scores decay without reinforcement.
- Future schema concept: `brain_trust_scores`, `topic_trust_scores`.

#### 4. Human-controlled learning
- When the federation layer finds a meaningful connection or belief shift, the
  local dashboard can notify the user: *"I learned X today — from peer Y."*
- The user can **accept**, **reject**, **mark contested**, or **ask for more
  evidence** from the dashboard review queue.
- Nothing from a peer is silently promoted without the write policy and, for
  conflicts, human review.
- Future schema concept: `human_review_queue`.

#### 5. Privacy and sharing policy
- Default behaviour: **private**. Nothing leaves the local brain unless
  explicitly marked as shareable/public.
- Claims that must never be shared: personal preferences, proprietary project
  details, secrets, credentials, client data, `DATABASE_URL`, API keys.
- Future schema concept: `sharing_policy` field on atoms or scopes.

### Future schema concepts (not yet designed)

These are placeholders for design thinking. None are implemented or planned for
the near term.

| Concept | Purpose |
|---|---|
| `peer_brains` | Registry of known peers: address, public key, last-seen, trust level |
| `external_signals` | Incoming claims from peers, before reconciliation |
| `brain_trust_scores` | Per-peer global trust score (float, decays) |
| `topic_trust_scores` | Per-peer per-topic trust score |
| `claim_hashes` / canonical claims | Content-addressed deduplication across brains |
| `belief_revisions` | Explicit log of why a belief changed (augments existing lifecycle fields) |
| `sharing_policy` | Per-atom or per-scope visibility flag |
| `human_review_queue` | Pending peer-sourced candidates awaiting user accept/reject |

### What this is NOT

- This is not a distributed database or a global shared brain.
- This is not a social network or a public knowledge graph.
- This is not a replacement for the local write policy — the write policy applies
  to all incoming data regardless of source.
- Peer brains do not have write access to each other's databases.

### Prerequisites before starting Phase 8

1. Local memory stable (Phases 1–6.5) ✅
2. Task reflection and model adaptation (Phase 7) ✅
3. Web research fallback with provider abstraction (Phase 7.6) ✅
4. Dashboard review flow for contested/proposed atoms — fully tested
5. Sharing policy design reviewed for privacy risks
6. Threat model for P2P claim injection (spam, poisoning, replay)

---

## Future Considerations

### Scaling vector search

Exact cosine search via pgvector is intentional for prototype scale. A few things
to know before planning an ANN index:

- The current embedding model (`qwen3-embedding:latest`) returns **4096-dimensional vectors**.
- Standard pgvector HNSW and IVFFlat indexing is not available for `vector(4096)` —
  pgvector's ANN index support has dimensionality limits below this.
- The doctor check warns about the absent ANN index; this is expected and acceptable
  at prototype scale.

Future scaling options when atom counts warrant it:
- Switch to a lower-dimensional embedding model (e.g. 768-d or 1536-d) and rebuild embeddings.
- Apply dimensionality reduction before indexing.
- Evaluate `halfvec` or other pgvector storage strategies if dimension limits change.
- Move ANN search to a dedicated vector database if pgvector becomes a bottleneck.

Do not create a direct HNSW index on the current `vector(4096)` column — it is not supported.

---

## MCP Usability Improvements (Copilot Friction Log)

Observations from live usage of the MCP server during active development sessions.
Check items off as they are completed.

---

- [x] **1. Scope discovery — Copilot cannot call `memory_project_context` without already knowing the scope string**

  The primary Copilot session-start pattern is `memory_project_context(scope="<project>")`, but
  there is currently no way to discover what scope strings exist in the store. In practice,
  Copilot has to ask the user directly.

  **Options (pick one):**
  - Add `available_scopes: list[str]` to the `memory_health` output — one `SELECT DISTINCT scope`
    query, no new tool required. Cheapest fix.
  - Add a standalone `memory_list_scopes` tool if scopes need richer metadata (counts, last-updated).
  - At minimum, document that the user must pass the scope string to Copilot before
    `memory_project_context` is useful.

  **Recommended:** Add `available_scopes` to `memory_health` first; promote to a dedicated tool
  only if scope metadata becomes useful later.

---

- [x] **2. `memory_search` has no minimum similarity threshold — low-similarity results add noise**

  A single query can return results ranging from similarity `0.73` down to `0.34`. There is no
  way to filter low-confidence matches server-side. Copilot has to judge relevance by eye, which
  is unreliable at the tail end of results.

  **Fix:** Add an optional `min_similarity` float parameter to `memory_search` (default `0.0`,
  clamped `0.0–1.0`). Apply server-side as a post-filter after the cosine search. No breaking
  change — default preserves current behavior.

  **Suggested default for Copilot use:** `0.45` filters obvious noise while keeping useful
  lower-confidence results. Document a recommended value in the tool description.

---

- [x] **3. `memory_recent` not implemented — Copilot cannot browse the store without a search query**

  Without `memory_recent`, there is no way to answer "what has the user stored lately?" or
  orient to a project at session start without already having a query in mind. It is the
  simplest unimplemented tool in the spec: `ORDER BY created_at DESC LIMIT n`.

  **Priority note:** Implement `memory_recent` before MCP write tools. It directly unblocks
  basic Copilot orientation and completes the planned read-only tool set.

---

- [x] **4. `context_summary` vs `content` not documented for Copilot consumers**

  Every MCP tool returns both `content` (full canonical sentence) and `context_summary`
  (compact, prompt-friendly version). The distinction exists to manage context window pressure,
  but nothing in the current docs or tool descriptions tells Copilot which field to prefer.

  **Fix:** Add to the MCP section and to the tool docstrings:
  > *Prefer `context_summary` for prompt injection and display. Use `content` only when exact
  > canonical wording matters (e.g. when citing or comparing memories).*

  Doc and docstring change only — no code required.

---

- [x] **5. Write report shape is unspecified for the CLI auto-store path**

  The MCP tool `memory_store_auto` has a pinned output shape in `mcp_integration_plan.md`.
  The CLI auto-store path (the execution branch about to be implemented in Phase 3) has no
  equivalent contract. If these diverge during implementation, Copilot's write report parsing
  will break.

  **Fix:** Before implementing the CLI auto-store path, define the canonical write report shape
  in one place and reference it from both the CLI and MCP implementations.

  **Minimum required fields:**
  ```json
  {
    "stored": true,
    "memory_atom_id": "uuid",
    "memory_signal_id": "uuid",
    "content": "string",
    "memory_type": "string",
    "scope": "string",
    "relationship": "new|refinement",
    "signal_created": true,
    "auto_stored": true
  }
  ```

---

## Architecture Diagram (Conceptual)

```
┌─────────────────────────────────────────────┐
│           User / Copilot Input              │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────▼─────────┐
         │  LLM Extraction   │  (qwen3:8b)
         └─────────┬─────────┘
                   │ candidates
         ┌─────────▼─────────┐
         │  Embedding +      │  (qwen3-embedding:latest)
         │  pgvector search  │  (find related existing atoms)
         └─────────┬─────────┘
                   │ related atoms
         ┌─────────▼─────────┐
         │  LLM Reconciler   │  (new / duplicate / conflict / ...)
         └─────────┬─────────┘
                   │ relationship + reason
         ┌─────────▼─────────┐
         │  Write Policy     │  (auto-store / ask / skip)
         └─────────┬─────────┘
                   │ approved
    ┌──────────────▼──────────────────┐
    │  Single transaction             │
    │  INSERT memory_atoms            │
    │  INSERT memory_signals (linked) │
    └──────────────┬──────────────────┘
                   │
         ┌─────────▼─────────┐
         │  Postgres         │  (source of truth)
         │  memory_atoms     │  ◄── MCP read tools query here
         │  memory_signals   │  (immutable evidence)
         └───────────────────┘
                   ▲
                   │ retrieval context
         ┌─────────┴─────────┐
         │  MCP Server       │  (stdio, VS Code / Copilot)
         │  memory_search    │  ← includes aggregate fields
         │  memory_health    │    + disagreement_flag
         │  memory_get       │
         │  memory_get_      │
         │  signals          │  ← provenance inspection
         └───────────────────┘
```
