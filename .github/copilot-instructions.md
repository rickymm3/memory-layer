# Copilot Mandatory Workflow — memory-layer

> **THESE RULES ARE NON-NEGOTIABLE. THEY APPLY TO EVERY SINGLE MESSAGE IN THIS WORKSPACE, NO EXCEPTIONS.**
> Skipping any step is a workflow violation. Do not rationalize skipping because the task feels unrelated, urgent, or off-topic.

---

## STEP 1 — YOU MUST search memory BEFORE responding

**DO NOT TYPE A SINGLE WORD OF YOUR RESPONSE until you have called `memory_search`.**

```
memory_search(
    query="<user's message verbatim or lightly paraphrased>",
    scope="project:memory-layer",
    min_similarity=0.45
)
```

**Only permitted skip condition:** purely mechanical language lookups with zero project context (e.g. "what does len() do?"). When in doubt, call it anyway. Being focused on an unrelated task is NOT a reason to skip. Being in the middle of a long task is NOT a reason to skip.

If you find yourself about to respond without having called `memory_search` — STOP. Call it first. Then respond.

---

## STEP 2 — YOU MUST log the turn AFTER responding

After every turn where you called any memory tool (`memory_search`, `memory_get`,
`memory_store_auto`, `memory_reflect_turn`, etc.), call `memory_log_turn` immediately:

```
memory_log_turn(
    user_message="<the user's message>",
    assistant_response="<your response>",
    retrieved_atom_ids=["<uuid>", ...],   # all IDs returned by memory_search
    used_atom_ids=["<uuid>", ...],        # subset that shaped your response
    context_status="sufficient",          # or: insufficient | stale | conflicting
    verdict="approved",                   # or: needs_caveat | needs_revision
    confidence=0.85,
    reasoning="<one sentence: how memory influenced this response>"
)
```

**Skipping this makes the entire conversation invisible to the user in the dashboard. That is unacceptable.**

---

## STEP 3 — YOU MUST reflect on durable insights AFTER responding

After any turn that produced facts, decisions, preferences, fixes, or lessons learned, call `memory_reflect_turn`:

```
memory_reflect_turn(
    user_msg="<the user's message>",
    answer="<your response>",
    scope="project:memory-layer"
)
```

Do not decide "this turn wasn't important enough." If the user told you something, fixed something, or you learned something — commit it. Err on the side of storing.

---

## VIOLATION CHECKLIST — run this before finishing every turn

- [ ] Did I call `memory_search` BEFORE writing my response?
- [ ] Did I call `memory_log_turn` AFTER responding?
- [ ] Did I call `memory_reflect_turn` if anything durable was learned?

If any box is unchecked and the skip condition does not apply — you have violated this workflow. Acknowledge it and call the missing tool now before the turn ends.

---

## Summary

| Order | Action | Tool | Skip when |
|-------|--------|------|-----------|
| **BEFORE** responding | Retrieve context | `memory_search` | Purely mechanical lookup — nothing else qualifies |
| **AFTER** responding | Audit log | `memory_log_turn` | No memory tools used at all |
| **AFTER** responding | Store insights | `memory_reflect_turn` | Truly ephemeral, nothing learned |

Full workflow details: `prompts/memory-layer-workflow.instructions.md`
