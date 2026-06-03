# MCP Integration Plan: memory-layer × GitHub Copilot

## 1. Goal

Connect the local memory-layer to GitHub Copilot in VS Code using the Model Context Protocol (MCP).

Copilot should be able to retrieve project constraints, architectural decisions, domain facts, and hard-won lessons from the memory-layer while the user is coding — without leaving the editor and without disrupting the existing CLI workflow.

**What this enables:**

- Copilot answers questions grounded in locally stored project knowledge.
- The user does not have to re-explain the same constraints in every chat session.
- Memory atoms accumulated through `chat_session.py` become available as live context during coding.

**What this does not change:**

- `chat_session.py` remains the primary interface for learning, extraction, and storage.
- Postgres rows remain the single source of truth.
- Embeddings remain semantic pointers, not data themselves.
- `memory_atoms` remain the retrieval source for both chat and MCP.
- `memory_signals` remain historical evidence, written only through confirmed storage flows.

The MCP server is an adapter layer. It translates Copilot tool calls into queries against the existing memory-layer database. It does not own data, does not mutate state, and does not bypass the memory-layer write policy.

---

## 2. Non-Goals for First MCP Version

The following are explicitly out of scope for the initial implementation:

| Non-goal | Reason |
|---|---|
| Unconstrained automatic memory writes | MCP write tools (future) must route each candidate through the memory-layer write policy; policy determines auto-store with report, confirmation-required, or skip — MCP cannot bypass this routing |
| Memory mutation through MCP | v1 MCP is read-only; atoms may still be manually edited through existing local tools, while signals are intended to be immutable evidence records |
| Signal aggregation or scoring | Phase 3+ concern |
| Replacing `chat_session.py` | CLI system remains the canonical memory interface |
| Cloud dependency | Entire stack runs locally: Ollama, Postgres, MCP server |
| Multi-user or networked access | Local process only; no network listener or multi-user access in v1 |
| Real-time extraction during coding | Extraction is a deliberate, user-initiated act |

---

## 3. Initial Read-Only MCP Tools

The planned read-only MCP interface includes five tools. The first implementation milestone exposed `memory_health` and `memory_search`. `memory_project_context` is now also implemented. The remaining two tools (`memory_get`, `memory_recent`) are fully defined here and planned for a subsequent read-only milestone. All tools query `memory_atoms` only. No tool touches `memory_signals`, executes shell commands, runs arbitrary SQL, or exposes secrets.

---

### `memory_health`

**Purpose:** Confirm that the memory-layer is reachable and report basic statistics.

**Inputs:** None.

**Output shape:**
```json
{
  "status": "ok",
  "atom_count": 142,
  "embedding_model": "qwen3-embedding:latest",
  "db_reachable": true
}
```

**Safety constraints:**
- No query parameters accepted.
- Never exposes connection strings, credentials, or file paths.
- Returns `"status": "error"` with a short message on failure; no stack traces.

---

### `memory_search`

**Purpose:** Find memory atoms semantically relevant to a query string. Primary tool for Copilot to retrieve context while coding.

**Inputs:**
```json
{
  "query": "string — natural language question or topic",
  "limit": "integer, optional, default 5, max 20",
  "scope": "string, optional — filter by scope (e.g. 'project', 'global')",
  "memory_type": "string, optional — filter by type (e.g. 'fact', 'decision', 'constraint')"
}
```

**Output shape:**
```json
[
  {
    "id": "uuid",
    "content": "string",
    "context_summary": "string",
    "memory_type": "string",
    "scope": "string",
    "confidence": 0.9,
    "importance": 0.8,
    "similarity": 0.87,
    "created_at": "ISO 8601"
  }
]
```

**Safety constraints:**
- Query string is used only for embedding generation and cosine similarity search. No raw SQL injection vector.
- `limit` is clamped server-side to a maximum of 20.
- No filter input is interpolated into raw SQL; all filters use parameterized queries.
- Embedding model is fixed from server config; not user-configurable per request.

---

### `memory_project_context`

**Purpose:** Return a curated snapshot of high-importance, high-confidence atoms for a given scope. Intended to give Copilot a broad orientation to a project before a coding session begins.

**Inputs:**
```json
{
  "scope": "string — required",
  "limit": "integer, optional, default 10, max 30",
  "min_importance": "float, optional, default 0.6",
  "min_confidence": "float, optional, default 0.7"
}
```

**Output shape:** Same row shape as `memory_recent`.

**Safety constraints:**
- `scope` is parameterized.
- Threshold floats are validated server-side (must be 0.0–1.0).
- `limit` clamped to 30.

---

> **Planned after v1** — The following two tools are defined here for completeness. They are not part of the current implementation.

### `memory_get`

**Purpose:** Retrieve a single memory atom by ID.

**Inputs:**
```json
{
  "id": "uuid"
}
```

**Output shape:**
```json
{
  "id": "uuid",
  "content": "string",
  "context_summary": "string",
  "memory_type": "string",
  "scope": "string",
  "confidence": 0.9,
  "importance": 0.8,
  "created_at": "ISO 8601"
}
```
Returns `null` if the ID is not found.

**Safety constraints:**
- ID is passed as a parameterized query parameter; no SQL injection vector.
- Returns only columns explicitly projected. No wildcard `SELECT *`.

---

### `memory_recent`

**Purpose:** List the most recently stored memory atoms, newest first. Useful for Copilot to understand what was learned in recent sessions.

**Inputs:**
```json
{
  "limit": "integer, optional, default 10, max 50",
  "scope": "string, optional",
  "memory_type": "string, optional"
}
```

**Output shape:** Same row shape as `memory_search`, without the `similarity` field.

**Safety constraints:**
- `limit` clamped to 50 server-side.
- All filters parameterized.
- No cursor or pagination token in v1; simple `ORDER BY created_at DESC LIMIT n`.

---

## 4. Later Write / Proposal Tools

These tools are planned for a future phase after the read-only server is stable and in active use. They are documented here to confirm the design intent. **None of these should be implemented in the first milestone.**

**Proposed future write flow:**

1. Copilot calls `memory_extract_candidates` with a text snippet → candidates returned, nothing stored.
2. Copilot calls `memory_reconcile_candidate` for each candidate → relationship classification and reason returned, nothing stored.
3. The memory-layer write policy routes each candidate based on the reconciliation result:
   - `duplicate` or `reinforcement` → **skip**: nothing stored, no report produced.
   - `new` or `refinement` (low risk) → **auto-store path**: proceed to step 4.
   - `conflict`, `opinion_change`, sensitive/personal claim, or high-impact instruction → **confirmation path**: proceed to step 5.
4. **Auto-store path:** Copilot calls `memory_store_auto` → stores `memory_atom` + linked `memory_signal` in a single transaction → returns a structured write report to Copilot and the user.
5. **Confirmation path:** Copilot calls `memory_propose_signal` → returns a proposal ID. User reviews the candidate, its relationship to existing memories, and the reconciliation reason in the local CLI or UI, then confirms or rejects. On confirmation, the CLI issues a short-lived token and Copilot calls `memory_store_approved` with the token → stores `memory_atom` + linked `memory_signal` in a single transaction → returns a write report.
6. **No silent writes**: every storage event — auto or confirmed — returns a write report containing at minimum: memory id, content, type, scope, and whether a linked signal was created.

Reconciliation (step 2) must complete before any routing decision (step 3). The user sees the extracted candidate *and* its reconciliation result before any write or proposal occurs.

---

### `memory_extract_candidates`

**Purpose:** Ask the memory-layer LLM pipeline to extract candidate memories from a provided text snippet. Returns candidates; does not store anything.

**Inputs:** `{ "text": "string", "context": "string, optional" }`

**Output:** List of extraction candidates with type, scope, confidence, and importance fields — matching the shape used in `chat_session.py`.

---

### `memory_reconcile_candidate`

**Purpose:** Run the reconciler against existing atoms for a single candidate. Returns relationship classification and reason. Does not store anything.

**Inputs:** A single candidate object (content, type, scope, confidence).

**Output:** `{ "relationship": "new|reinforcement|conflict|...", "reason": "string", "matched_ids": [] }`

---

### `memory_store_auto`

**Purpose:** Store a low-risk candidate (relationship `new` or `refinement`) that the write policy has routed to the auto-store path. Writes `memory_atom` + linked `memory_signal` in one transaction. Returns a structured write report; never stores silently.

**Inputs:** Candidate + reconciliation output.

**Output:** (see [Canonical Write Report Schema](#canonical-write-report-schema))
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

**Safety constraints:**
- Only valid for relationships `new` and `refinement`; must reject `conflict` and `opinion_change` inputs.
- Always returns a write report; never a silent side-effect.
- Writes nothing if validation fails; returns an error instead.

---

### Canonical Write Report Schema

Both the CLI auto-store path and the MCP `memory_store_auto` / `memory_store_approved` tools
must return a write report with at minimum these fields. Implementations must not omit fields
or invent alternate shapes — Copilot parses this output.

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

- `auto_stored`: `true` for write-policy auto-store path; `false` for confirmation-path stores.
- `signal_created`: always `true` when a linked `memory_signal` row was inserted.
- `relationship`: the reconciler's output — `new` or `refinement` only on this path.

---

### `memory_propose_signal`

**Purpose:** Record intent to store a candidate that the write policy has routed to the confirmation path. Does not insert into `memory_atoms` or `memory_signals`. Returns a proposal ID that can be reviewed in the CLI.

**Inputs:** Candidate + reconciliation output.

**Output:** `{ "proposal_id": "uuid", "status": "pending_review" }`

Only invoked when the write policy routes a candidate to the confirmation path (conflicts, opinion changes, sensitive claims, high-impact instructions). Low-risk new candidates use `memory_store_auto` instead.

---

### `memory_store_approved`

**Purpose:** Store a candidate that was routed to the confirmation path and has been manually approved. Requires a short-lived confirmation token issued by the CLI review step. Returns a write report.

**Safety note:** In v1, MCP is entirely read-only and this tool does not exist. For future write phases, this tool must require a confirmation token issued by the CLI review step. Copilot cannot generate valid tokens; it can only initiate a proposal and present candidates for review. The token is the mechanism that ensures the write policy confirmation path cannot be bypassed — no confirmation-path write may proceed without one. Relaxing this requirement is a deliberate protocol decision, not an implementation shortcut.

---

## 5. VS Code / Copilot Integration

### Transport: stdio (v1)

The first version runs as a **stdio MCP server**. VS Code launches the process directly and communicates over stdin/stdout. No port is bound, no HTTP server is required, and no background daemon needs to be running before opening the editor.

VS Code is configured via `.vscode/mcp.json` (workspace-scoped):

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

Server naming uses camelCase (`memoryLayer`) to match VS Code MCP configuration conventions. The `envFile` field loads `DATABASE_URL` and other secrets from the existing `.env` file so they are never hardcoded in the config.

VS Code starts the process on demand when Copilot needs a tool. The process exits when the session ends. No persistent daemon is required for the stdio transport.

For manual local testing outside VS Code, a `make mcp` target can start the same entry point in a terminal.

Once registered, Copilot can invoke any exposed tool during a chat session using `#memoryLayer` references or automatic tool selection when the tool descriptions match the question being asked.

**Expected usage pattern during coding:**

1. Developer opens VS Code in a project that has `.vscode/mcp.json` checked in or placed locally.
2. VS Code launches `mcp_server.server` as a subprocess on first tool use.
3. Copilot chat session begins. Copilot calls `memory_project_context(scope="myproject")` automatically or on developer prompt.
4. Relevant atoms are returned as grounding context for subsequent code questions.
5. Developer asks a question; Copilot calls `memory_search` for additional retrieval as needed.
6. No writes occur. All new learning happens through `chat_session.py` outside VS Code.

### Future Transport: HTTP / localhost (optional)

HTTP over localhost (e.g. port `3333`) may be added later as an alternative transport for:
- Other local clients or tooling that prefer HTTP.
- Long-running service mode where startup latency matters.
- Dashboards or debugging interfaces.

HTTP is not required for the first VS Code/Copilot integration and is not part of the v1 milestone.

---

## 6. Security Model

| Constraint | Implementation |
|---|---|
| Read-only first | No `INSERT`, `UPDATE`, or `DELETE` executed by the MCP server in v1 |
| Local only | Launched locally by VS Code through stdio; no network port bound in v1 |
| No shell execution | MCP handlers call Python functions only; no `subprocess`, `os.system`, or shell eval |
| No arbitrary SQL | All queries are hardcoded parameterized statements; no user-supplied SQL fragments |
| Write policy enforced | Write tools do not exist in v1; future write tools route each candidate through the memory-layer write policy (auto-store with report, confirmation-required, or skip) — Copilot cannot bypass this routing |
| Debug output separation | Debug logs go to stderr only; MCP tool responses on stdout contain only structured data |
| Secrets never exposed | Connection strings, API keys, and file paths are never included in tool output |
| Input validation | All numeric inputs clamped; string inputs are query parameters only, never interpolated |
| No cross-tool state | Each tool call is stateless; no session tokens, no shared mutable state between calls |

The MCP server process has the same OS-level permissions as the user running it. It does not drop privileges and does not require elevated access. The Postgres connection uses `DATABASE_URL` loaded from `.env` via the `envFile` field in `.vscode/mcp.json`; the secret is never visible in tool output or logs sent to stdout.

---

## 7. First Implementation Milestone

**Scope:** Minimal viable stdio MCP server with two tools.

**Deliverables:**

1. `mcp_server/` package directory with:
   - `server.py` — stdio MCP server entry point (`python -m mcp_server.server`)
   - `tools/health.py` — `memory_health` handler
   - `tools/search.py` — `memory_search` handler
2. `.vscode/mcp.json` with stdio configuration (see Section 5).
3. `Makefile` target `mcp` for manual local testing outside VS Code.
4. MCP SDK added to `requirements.txt`; no HTTP server library required for v1.
5. `scripts/check_environment.py` updated so `make doctor` can verify the `mcp_server` package is importable.

**Acceptance criteria:**

- `python -m mcp_server.server` starts without errors.
- VS Code launches the server via `.vscode/mcp.json` and Copilot can invoke tools.
- `memory_health` returns `"status": "ok"` when Postgres and Ollama are reachable.
- `memory_search` returns semantically relevant atoms for a test query issued from Copilot chat in VS Code.
- All existing tests and `make doctor` continue to pass.
- No writes occur under any input.
- `chat_session.py` and `extract_and_store_memory.py` are unmodified by this milestone.

**Not in this milestone:** HTTP transport, `memory_get`, `memory_recent`, any write/proposal tools.
