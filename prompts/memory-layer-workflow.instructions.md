---
applyTo: "**"
---

# memory-layer Workflow Instructions

These instructions govern how to use the memory-layer MCP server. They apply
to **every exchange** — coding tasks, planning, discussion, and questions alike.
Durable knowledge (project decisions, constraints, facts, lessons, opinions)
lives inside memoryLayer as atoms and is retrieved when relevant.

---

## Every message — semantic retrieval first

**Before responding to any user message**, call `memory_search` using the
user's message as the query. This fires a semantic embedding search against all
stored atoms and surfaces anything relevant — project constraints, prior
decisions, stated preferences, known model weaknesses, or corrections.

```
memory_search(
    query="<user's message, verbatim or lightly paraphrased>",
    scope="project:memory-layer"   # adjust slug to the active project
)
```

Use the results to inform your response. If an atom is relevant, treat it as
ground truth for this project unless the user explicitly overrides it. Do not
ask the user to repeat context they have already stored.

Skip this step only for:
- Purely mechanical lookups with no project context (e.g. "what does `len()` do?")
- When the user's message is a direct continuation of your previous tool output

---

## Session start

At the start of every coding task, before planning or editing any files, call
`memory_task_context` to load project constraints, model lessons, and recent
task history in a single call:

```
memory_task_context(
    project_scope="project:<slug>",
    model_scope="model:<name>",   # omit if model scope is unknown
    task_hint="<one sentence describing the task>"  # omit if no specific task yet
)
```

Replace `<slug>` with the project identifier (e.g. `project:my-rails-app`,
`project:memory-layer`). Replace `<name>` with the active model identifier
(e.g. `qwen3-8b`). The response has four sections:

- `project_context` — high-importance project atoms (constraints, decisions)
- `model_lessons` — known weaknesses and prompt adaptations for this model
- `recent_task_runs` — last N task outcomes for this project
- `task_relevant_atoms` — semantic matches for `task_hint` (only when provided)

Do not skip this step. Stored context catches constraints and known failure
patterns that are not obvious from the code alone.

### Readiness assessment

After `memory_task_context` returns, evaluate whether you have enough context
to act before touching any files. Call `memory_assess_task_readiness` with the
same scope and task description:

```
memory_assess_task_readiness(
    task_description="<one sentence describing what you are about to do>",
    project_scope="project:<slug>",
    model_scope="model:<name>",   # same as above; omit if unknown
    task_hint="<same hint as above>"  # omit if task_hint was not used
)
```

**Act on `recommended_action` before proceeding. Do not proceed if `ready` is
`false`.**

| `recommended_action` | What to do |
|---|---|
| `proceed` | Context is sufficient — begin the task |
| `project_kickoff` | Call `memory_project_kickoff` to bootstrap project context first |
| `inspect_conflict` | Review the contested atoms listed in `risks[]` before coding |
| `search_web` | Retrieve current/external information (use web search or ask the user) |
| `define_tests` | Clarify test/verification requirements with the user before coding |
| `retrieve_more` | Call `memory_search` with `suggested_search_query` to fill the gap |
| `ask_user` | Ask the user for the missing context listed in `missing[]` |

Check `required_checks[]` before starting — each entry is a concrete pre-task
action (e.g. "Define test requirements before coding begins", or a model lesson
about known weaknesses).

**Alternatives for specific use cases:**

- `memory_project_context(scope="project:<slug>")` — project atoms only; use
  when you only need constraints and have no model scope
- `memory_search(query="<topic>", scope="project:<slug>")` — targeted semantic
  search; use mid-task when a specific question arises
- `memory_list_task_runs(scope="project:<slug>")` — task run history only

---

## Memory-capture evaluation

After completing a task — or when a user states something durable mid-session —
evaluate the conversation for content worth storing. Not everything should be
stored.

### Preferred: single-call end-of-turn capture

For most turns, use `memory_reflect_turn` instead of the manual pipeline below.
It extracts, reconciles, and commits in one call:

```
memory_reflect_turn(
    user_msg="<the user's message>",
    answer="<your response, think-block stripped>",
    thinking="<chain-of-thought text if available, else empty string>",
    scope="project:<slug>"   # omit to let the extractor assign scope per candidate
)
```

Call this at the end of turns that produced new facts, decisions, preferences,
or lessons. Do **not** call it for purely ephemeral exchanges (quick lookups,
restatements, clarifying questions).

### Logging turns to the dashboard prompt history

After `memory_reflect_turn` (or any turn where you called `memory_search`,
`memory_get`, or `memory_store_auto`), also call `memory_log_turn` so the turn
appears in the dashboard under **Prompt History**:

```
memory_log_turn(
    user_message="<the user's message>",
    assistant_response="<your response, think-block stripped>",
    retrieved_atom_ids=["<uuid>", ...],   # all atom IDs returned by memory_search this turn
    used_atom_ids=["<uuid>", ...],        # subset that actually shaped your response
    context_status="sufficient",          # or: insufficient | stale | conflicting | unsupported
    verdict="approved",                   # or: needs_caveat | needs_revision | needs_verification
    confidence=0.85,
    reasoning="<one sentence: how retrieved memory influenced this response>"
)
```

`retrieved_atom_ids` / `used_atom_ids`: pass `[]` if no atoms were retrieved.
Pass `[]` for `used_atom_ids` if retrieved atoms were not relevant to the answer.

Call `memory_log_turn` on **every** turn where memory tools were used — not
only turns where new memories were stored. This is how the dashboard accumulates
a full audit trail of what context was available and how it was used.

The manual extract → reconcile → store pipeline below is still available for
fine-grained control — use it when you need to inspect or override individual
candidates before committing.

### Capture candidates (consider storing)

- Explicit user corrections: "don't do X", "always use Y for Z"
- Stated preferences that apply beyond this session and this chat
- Project-level decisions that future tasks must respect
- Facts the user has confirmed as ground truth for this project
- Observed model weaknesses or prompt adaptations that improved outcomes
- Workflow instructions the user wants applied consistently

### Do NOT capture — skip silently

- Temporary decisions: "just for now", "this time only", "for this file"
- Vague or context-free statements without a clear actionable meaning
- Personal, sensitive, or identifying information of any kind
- Intermediate reasoning steps that only apply to the current task
- Anything the user has not confirmed or explicitly stated
- Content that contradicts a stored atom — propose a signal for review instead
  (do not auto-store conflicts)
- Secrets, credentials, connection strings, API keys — never

When in doubt, skip. A missed lesson can be stored later; an incorrectly stored
claim is harder to clean up.

---

## Scope classification

Classify each candidate into exactly one scope before storing:

| Scope | When to use |
|---|---|
| `project:<slug>` | Applies to this codebase only — conventions, stack decisions, constraints, project lessons |
| `model:<name>` | Observations about a specific model's behavior — weaknesses, prompt adaptations, reliability patterns |
| `user` | User-wide preferences — communication style, habits, general cross-project workflow |
| *(skip — current chat only)* | One-off context with no durable value beyond this conversation |

Default to `project:<slug>` for coding task context.  
Do not write to `user` scope unless the user has explicitly expressed a
cross-project preference.  
Do not write to `model:<name>` scope without a concrete behavioral observation
(not just "the model did this once").

---

## Write pipeline

Follow this sequence for every candidate. Never bypass a step.

**1. Extract**
```
memory_extract_candidates(text="<user statement or task context>")
```
Returns a list of typed candidates. Pass user-authored content — not assistant
output — as the primary text.

**2. Reconcile each candidate**
```
memory_reconcile_candidate(
    content="<candidate sentence>",
    memory_type="<type>",
    scope="<scope>"
)
```
Check the `is_auto_storable` field in the response:
- `true` (relationship is `new` or `refinement`) → proceed to step 3
- `false` (conflict, opinion_change, ask_user, sensitive) → proceed to step 4

**3. Auto-store low-risk candidates**
```
memory_store_auto(
    content="<candidate>",
    memory_type="<type>",
    relationship="<new|refinement>",
    scope="<scope>",
    confidence=<float>,
    importance=<float>,
    reconciliation_reason="<reason from reconciler>",
    matched_memory_ids=[...]
)
```
Use the values from the reconciler response — do not invent them.

**4. Propose for review — conflicts and opinion changes**
```
memory_propose_signal(
    content="<candidate>",
    memory_type="<type>",
    relationship="<conflict|opinion_change|ask_user>",
    scope="<scope>",
    ...
)
```
These are queued for the user to review via `make review-proposals`. Do not
store them automatically. Do not mention them unless the user asks.

---

## Opinion and belief handling

User opinions, preferences, and beliefs are first-class memory. They are stored
and treated differently from facts — they can be updated, superseded, or
contradicted without either version being "wrong".

### When to capture opinions

Store an opinion or preference when the user:
- States a view about a tool, approach, technology, or practice
- Expresses a preference that will affect future decisions in this project
- Corrects or refines a previously stated preference

Use `memory_type="opinion"` for evaluative judgments  
Use `memory_type="preference"` for stated preferences  
Use `memory_type="belief"` for working assumptions the user treats as true

### Canonical phrasing

Always rewrite opinion candidates in attribution form before storing:

- `"The user believes <view>."`
- `"The user prefers <option> for <context>."`
- `"The user considers <X> to be <judgment>."`

This makes retrieval unambiguous: the memory is about the user's view, not a
general fact.

### Conflicts and opinion changes

If the user states a new opinion that contradicts a stored one, do NOT
auto-store the new version. Call `memory_reconcile_candidate` first — the
reconciler will return `relationship="opinion_change"` if it detects a
conflict with an existing belief. Then use `memory_propose_signal` to queue it
for review.

The user will resolve the conflict via `make review-proposals`. You will then
see the updated belief on the next turn.

### Inspecting a stored belief

To understand the full history of a stored belief (supporting/opposing signals,
revision log), call:

```
memory_get_belief(
    atom_id="<uuid>"   # or use query="<search term>" to find it by content
)
```

The response includes:
- `atom` — current belief state (confidence, lifecycle status, disagreement_score)
- `signals.supporting` — evidence that reinforces the belief
- `signals.opposing` — evidence that challenges it
- `revision_history` — how the belief has changed over time
- `belief_summary` — plain-English one-liner for quick inspection

Use this before writing code that depends on a preference that has a high
`disagreement_score` — the stored belief may be contested.

### Memory routing and stored corrections

When the assistant retrieves a `correction` or `warning` atom during a chat
turn, the RECURSIVE routing path is triggered automatically. The draft answer
is checked against the stored correction before being shown to the user. You
do not need to do anything special — this is handled by the chat pipeline.

If a correction atom causes the answer to change significantly from what you
expected, inspect the atom with `memory_get_belief` or the dashboard's
**Beliefs** view to understand its provenance.

---

Report every storage event immediately after it occurs, in this exact format:

```
[MEMORY STORED]
  memory_atom_id:   <uuid>
  memory_signal_id: <uuid>
  content:          <stored content>
  type:             <memory_type>
  scope:            <scope>
```

If a candidate is skipped (duplicate, low-value, or current-chat-only), do not
report it unless the user asks. Never report silently-skipped items as stored.

If a candidate is queued for review (conflict, opinion change), say:
```
[MEMORY QUEUED FOR REVIEW]
  content: <candidate>
  reason:  <why it needs review>
  action:  run `make review-proposals` to approve or reject
```

---

## Importing conversations from other LLMs

To bulk-import a conversation transcript from another LLM (Claude, GPT-4,
etc.) into the shared memory store, call:

```
memory_ingest_transcript(
    turns=[
        {"role": "user", "content": "..."},
        {"role": "assistant", "content": "..."},
        ...
    ],
    source_label="claude-3-7-sonnet",  # any descriptive label
    scope="project:<slug>"              # optional; omit to let extractor assign
)
```

Each user+assistant pair is processed through the full commit pipeline.
Returns `{committed, proposed, skipped, turns_processed, errors}`.

Alternatively, use the CLI: `python scripts/ingest_transcript.py --file turns.json --source <label> [--dry-run]`

---

## Task summary

After completing any non-trivial task, close with a brief summary:

1. **Done:** what was implemented, in 1–2 sentences.
2. **Next:** what the logical next step is, in 1 sentence.

Keep it tight. Do not pad with "I hope this helps" or similar closers.

---

## What these instructions are not

These instructions tell the agent **when and how** to use memoryLayer. They are
not a substitute for the memory stored inside it. If there is a conflict between
what these instructions say and what `memory_project_context` returns for this
project, the stored memory for this project takes precedence — it reflects
accumulated, corrected, project-specific knowledge.

To update these instructions for your own workflow, edit the installed copy in
your VS Code User prompts directory. To update the shared template, edit
`prompts/memory-layer-workflow.instructions.md` in the repository and re-run
`make install-vscode-prompts`.
