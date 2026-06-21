# memory-layer — Claude Code Workflow Instructions

This project has a live MCP server (`memoryLayer`). Follow these instructions at the start of every task.

---

## Session Start — Do This First

```
memory_task_context(
  project_scope = "project:memory-layer",
  model_scope   = "model:claude-sonnet-4-6",
  task_hint     = "<what you are about to do>"
)
```

Fallback (if unavailable): `memory_project_context` → `memory_search` → `memory_list_task_runs`

---

## Readiness Assessment

After context load, call `memory_assess_task_readiness(project_scope, task_description, model_scope)`. Do not proceed if `ready=false`.

| recommended_action | What to do |
|---|---|
| proceed | Safe to start |
| retrieve_more | `memory_search` with more specific query |
| project_kickoff | `memory_project_kickoff` to capture missing context |
| inspect_conflict | `memory_get_signals` on contested atom |
| define_tests | Ask user for test expectations before coding |
| search_web | Fetch current docs/specs |

---

## Scope Classification

| Content type | Scope |
|---|---|
| Project decisions, constraints, patterns | project:memory-layer |
| Observations about this model's behaviour | model:claude-sonnet-4-6 |
| User preferences across all projects | user |
| Current-session only | skip — do not store |

---

## Write Pipeline

1. `memory_extract_candidates(text="...")` — extract candidates
2. `memory_reconcile_candidate(candidate_content, scope)` — find relationship
3. Route: new/refinement → `memory_store_auto` | conflict/opinion_change → `memory_propose_signal` | duplicate → skip

Do NOT capture: ephemeral task state, vague content, secrets, session-internal names (Phase N, Sprint N, "as discussed").

---

## Reporting Writes

Every storage event must report: `memory_atom_id`, `memory_signal_id`, `content`, `type`, `scope`, `relationship`.

---

## Per-Turn Conversational Memory — Non-Negotiable

I am the extractor. Write directly via `memory_store_auto` after any turn where the user stated a preference, correction, decision, or instruction. Every atom must include: WHAT + WHY + CONTEXT + REVISABILITY.

Triggers: preferences with reasons, architecture decisions, corrections, frustration/satisfaction signals, facts I would otherwise forget.

Fast path: judge → `memory_store_auto(content, memory_type, scope, importance, relationship)` → report both IDs.

Conflicts → `memory_propose_signal`. Duplicates/reinforcements → skip silently.

---

## End-of-Task Reflection

```
make reflect ARGS="--scope project:memory-layer \
  --task '<description>' \
  --files '<files changed>' \
  --outcome success|partial|failed \
  --notes '<lessons>' \
  --store"
```

---

## Key Invariants (Never Violate)

- Postgres rows are the source of truth. Embeddings are pointers.
- Signals are immutable. Never mutate after creation.
- No silent writes. Every storage event is reported with both IDs.
- All writes are dual: one memory_atom + one linked memory_signal per transaction.
- No secrets in MCP output. Connection strings and API keys never appear in tool responses.
- Parameterized SQL only. No f-string interpolation of user input in any query.
- No direct HNSW index on vector(4096). Exact cosine search only.

---

## Useful CLI Commands

```
make doctor          # verify full stack
make session         # interactive chat with memory
make list            # recent atoms
make list-signals    # recent signals
make list-task-runs  # recent task runs
make dashboard       # Flask UI on port 5001
```
