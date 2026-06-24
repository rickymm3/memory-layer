---
description: >
  Synapse autonomous development loop. On every iteration: reads the primer,
  audits the codebase against the loop diagram, picks the highest-priority
  open node, implements it, tests it, commits it, and updates BACKLOG.md.
  Does not wait for user direction.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, mcp__memoryLayer__memory_store_auto, mcp__memoryLayer__memory_search
---

You are running autonomously on behalf of the user. Do not wait for direction.
Your job is to advance the Synapse project by closing open loop nodes.

---

## Step 1 — Load the operating framework

Read this file in full before doing anything else:
/home/ricky/memory-layer/.claude/commands/synapse-loop.md

This defines the architecture you are implementing. Every decision must trace
to a node in the core loop diagram.

---

## Step 2 — Load the backlog

Read this file:
/home/ricky/memory-layer/BACKLOG.md

It contains the ranked list of open loop nodes. The top item is what you work
on this iteration unless it is blocked — in which case take the next unblocked
item and note the blocker on the blocked one.

---

## Step 3 — Audit before acting

Before touching code, verify:
1. Is the top backlog item already partially implemented? Check the relevant
   files. Do not duplicate work.
2. Does the item connect to a named node in the core loop? If not, skip it
   and note the mismatch in BACKLOG.md.
3. Will implementing it preserve write integrity on all three surfaces?

---

## Step 4 — Implement

Do the actual work. Write the code, the migration, the template, the test.
Do not describe what could be done — do it.

Target: one complete, working, tested unit of functionality per iteration.
A unit is the smallest thing that closes a node in the loop — a working route,
a passing test suite, a wired UI state change.

If the item is too large for one iteration: implement the first logical slice,
commit it, update BACKLOG.md to reflect what was done and what remains.

---

## Step 5 — Test

Run the full test suite before committing:
```
source .venv/bin/activate && python -m pytest tests/ -q --tb=short
```

If tests fail: fix them. Do not commit broken code.
If the item requires a new test: write it.

---

## Step 6 — Commit

Commit with a message that names the loop node that was closed.
Format: `feat: <what was built> — closes [Node Name] loop node`

---

## Step 7 — Update BACKLOG.md

Mark the completed item as done with the commit hash.
If the item was partially completed, update its description to reflect
what remains. Add any new open nodes discovered during implementation.

---

## Step 8 — Session-close check (from the primer)

Ask: "Did this iteration close a gap or open one?"
If it opened one — name it and add it to BACKLOG.md before scheduling.

---

## Step 9 — Schedule next iteration

Use ScheduleWakeup:
- prompt: `<<autonomous-loop-dynamic>>`
- delaySeconds: 1800
- reason: "Synapse loop — completed [what you just built], next: [top of backlog]"
