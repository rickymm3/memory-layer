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

Fallback (if unavailable): `memory_search` with a topic query.

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

Call `memory_store_auto` directly with the content, type, scope, and relationship hint. The commit pipeline handles reconciliation, critic review, and conflict routing automatically — no pre-extraction or multi-step flow needed.

Do NOT capture: ephemeral task state, vague content, secrets, session-internal names (Phase N, Sprint N, "as discussed").

---

## Reporting Writes

Every storage event must report: `memory_atom_id`, `memory_signal_id`, `content`, `type`, `scope`, `relationship`.

---

## Per-Turn Conversational Memory — Non-Negotiable

I am the extractor. Write directly via `memory_store_auto` after any turn where the user stated a preference, correction, decision, or instruction. Every atom must include: WHAT + WHY + CONTEXT + REVISABILITY.

Triggers: preferences with reasons, architecture decisions, corrections, frustration/satisfaction signals, facts I would otherwise forget.

Fast path: judge → `memory_store_auto(content, memory_type, scope, importance, relationship)` → report both IDs.

**Scope discipline (critical):**
- Project facts → `scope="project:memory-layer"`
- Observations about MY behavior (patterns, weaknesses, what helps me) → `scope="model:claude-sonnet-4-6"`
- User preferences → `scope="user"`

Model lessons are written to `model:claude-sonnet-4-6` scope. Examples of model-scope content: "Claude Sonnet defaults to scope creep on bug fixes", "Claude performs better with adversarial audit framing", "Claude ignores conversational behavioral constraints over long sessions." Write these immediately when observed — they inform every future session via `memory_task_context(model_scope=...)`.

Conflicts → conflicts now route through signal math automatically (no proposal queue). Duplicates/reinforcements → skip silently.

---

## End-of-Task Reflection

```
make reflect ARGS="--scope project:memory-layer \
  --task '<description>' \
  --files '<files changed>' \
  --outcome success|partial|failed \
  --notes '<lessons>' \
  --model claude-haiku-4-5-20251001 \
  --store"
```

Note: `--model` overrides `CHAT_MODEL` for just this call. Required if `.env` has `CHAT_MODEL=qwen3:8b` (or any non-Anthropic model) — those models do not return structured JSON and will cause reflect to fail.

**Fast path — single observation (no LLM needed):**
```
make observe ARGS="--model claude-sonnet-4-6 \
  --content '<what you observed about the model>' \
  --injection '<verb-first directive to prepend to future prompts>' \
  --importance 0.8"
```
Use this when the ANTHROPIC_API_KEY is not set in `.env` or when capturing a single lesson immediately after an incident. Creates one memory_atom + one memory_signal (dual-write). Reports both IDs.

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
make doctor          # verify full stack (checks ANTHROPIC_API_KEY, CHAT_MODEL, etc.)
make session         # interactive chat with memory
make list            # recent atoms
make list-signals    # recent signals
make list-task-runs  # recent task runs
make dashboard       # Flask UI on port 5001
make model-report    # show model lessons with injection strings (make model-report ARGS="--model claude-sonnet-4-6")
make observe         # fast-path model lesson write (see End-of-Task Reflection above)
make users           # manage user identities (make users ARGS="list")
make purge-stale     # dry-run stale atom purge (add ARGS="--commit" to delete)
make verify          # end-to-end pipeline check: write quality + injection + behavioral coverage
```
