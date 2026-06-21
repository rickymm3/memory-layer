# memory-layer — Claude Code Workflow Instructions

This project has a live MCP server (`memoryLayer`) providing persistent memory
across sessions. Follow these instructions at the start of every task.

---

## Session Start — Always Do This First

Before planning or writing any code, call:
memory_task_context(

project_scope = "project:memory-layer",

model_scope   = "model:claude-sonnet-4-6",

task_hint     = "<short description of what you are about to do>"

)

This returns in one call:
- **project_context** — high-importance project decisions, constraints, and conventions
- **model_lessons** — known weaknesses and prompt adaptations for this model
- **recent_task_runs** — outcomes of prior tasks (avoid repeating past mistakes)
- **task_relevant_atoms** — semantically similar memories for the current task hint

If `memory_task_context` is unavailable, fall back in order:
1. `memory_project_context(scope="project:memory-layer")` — project knowledge
2. `memory_search(query="<task description>", scope="project:memory-layer")` — targeted search
3. `memory_list_task_runs(scope="project:memory-layer")` — recent task history

---

## Readiness Assessment — Before Touching Files

After retrieving context, call:
memory_assess_task_readiness(

project_scope    = "project:memory-layer",

task_description = "<what you are about to do>",

model_scope      = "model:claude-sonnet-4-6"

)

Do not proceed if `ready = false`. Act on `recommended_action`:

| recommended_action | What to do |
|---|---|
| proceed | Safe to start |
| retrieve_more | Call memory_search with a more specific query |
| project_kickoff | Call memory_project_kickoff to capture missing project context |
| inspect_conflict | Call memory_get_signals on the contested atom before deciding |
| define_tests | Ask the user to specify test expectations before coding |
| search_web | Use web search for current docs, API specs, or latest versions |

---

## Scope Classification

| Content type | Scope |
|---|---|
| Project decisions, constraints, patterns | project:memory-layer |
| Observations about this model's behaviour | model:claude-sonnet-4-6 |
| User preferences across all projects | user |
| Current-session only, not worth keeping | skip — do not store |

---

## Write Pipeline — How to Store a Memory

Only store concrete, durable, reusable facts. Run this pipeline:

1. Extract — `memory_extract_candidates(text="<instruction or decision>")`
2. Reconcile — `memory_reconcile_candidate(candidate_content="...", scope="...")`
3. Route by relationship:
   - new or refinement → `memory_store_auto(...)`
   - conflict or opinion_change → `memory_propose_signal(...)` (queued for CLI review)
   - duplicate or reinforcement → skip, no write

Do NOT capture:
- Temporary task-specific instructions
- Vague or ambiguous content
- Personal data, secrets, credentials, connection strings
- Content that only makes sense in this conversation

When in doubt, skip.

---

## Reporting Writes

Every storage event must be reported with these fields:

  memory_atom_id:   <uuid>
  memory_signal_id: <uuid>
  content:          "<full sentence>"
  type:             fact|decision|instruction|preference|opinion
  scope:            <scope>
  relationship:     new|refinement

---

## Per-Turn Conversational Memory — Non-Negotiable

Every conversation turn is a memory opportunity. At the end of any turn where
the user expressed a preference, opinion, decision, instruction, or correction,
I MUST write it to the memory layer before finishing my response.

**I am the extractor. Not qwen3:8b. Not a background process. Me.**

I see the conversation. I judge what is worth keeping. I write it directly.

Triggers that ALWAYS require a write:
- User states a preference ("I prefer X", "always use Y", "I like Z")
- User gives a reason or context for a preference ("because of X", "since we use Y")
- User corrects me or changes direction
- User makes a decision about architecture, tools, or process
- User expresses frustration or satisfaction (signals about what works)
- A fact was established that I would otherwise forget next session

Write pipeline for conversational turns (faster path than full task pipeline):
1. Judge: is this worth storing? If not, skip silently.
2. If yes: call `memory_store_auto` directly with:
   - content: the full fact, with WHY and CONTEXT included (see below)
   - memory_type: preference|opinion|decision|instruction|fact|correction
   - scope: user (cross-project preferences) or project:memory-layer (project-specific)
   - importance: 0.7–0.9 for explicit preferences/decisions, 0.5–0.7 for observations
3. Report the write (memory_atom_id + memory_signal_id).

Content quality rule — every stored atom must answer:
  - WHAT: the preference/decision/fact itself
  - WHY: the reason, if stated (even briefly)
  - CONTEXT: what they were working on, what triggered it
  - REVISABILITY: for preferences in fast-moving domains, note what would change it

Bad: "User prefers concise responses."
Good: "User prefers concise, direct responses and actively dislikes verbose explanations.
       Expressed while discussing memory layer architecture in June 2026. This is a strong
       ongoing preference — any response format change should be validated against it."

Conflicts with existing atoms go to `memory_propose_signal` for review.
Duplicates or reinforcements: skip the write, but note it internally.

---

## End-of-Task Reflection

After completing any non-trivial task, run:

make reflect ARGS="--scope project:memory-layer \
  --task '<short task description>' \
  --files '<files changed>' \
  --tests '<test results>' \
  --outcome success|partial|failed \
  --notes '<key observations or lessons>' \
  --store"

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

make doctor                 # verify full stack (expect 34 PASS, 1 WARN, 0 FAIL)
make session                # interactive chat with memory
make list                   # recent memory atoms
make list-signals           # recent memory signals
make list-task-runs         # recent task run records
make model-report ARGS="--model claude-sonnet-4-6"
make dashboard              # read-only Flask UI on port 5001
