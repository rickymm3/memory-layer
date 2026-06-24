---
description: >
  Synapse autonomous build loop. Goal: complete the project by closing every
  open node in BACKLOG.md. Runs continuously — each iteration implements one
  backlog item, commits it, and immediately fires the next iteration.
  Does not wait for user direction. Does not stop until the backlog is empty.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, mcp__memoryLayer__memory_store_auto, mcp__memoryLayer__memory_search
---

Your goal is to complete the Synapse project.

Read BACKLOG.md. Take the top unblocked item. Build it. Test it. Commit it.
Mark it done. Fire the next iteration immediately. Repeat until empty.

---

## Step 1 — Load the Synapse loop framework

Read this file completely before touching any code:
/home/ricky/memory-layer/.claude/commands/synapse-loop.md

This is your architecture spec. Every implementation decision must trace to a
node in the core loop diagram. If it doesn't, skip it and take the next item.

---

## Step 2 — Read the backlog

Read: /home/ricky/memory-layer/BACKLOG.md

Take the first item under **Active** that is not marked blocked.
If all items are blocked, write why in BACKLOG.md and stop the loop.
If the backlog is empty, the project is complete — stop the loop.

---

## Step 3 — Audit before coding

Before writing a single line:
1. Check if the item is already partially implemented. Read the relevant files.
   Do not duplicate work that exists. Continue from where it stopped.
2. Confirm it connects to a named node in the synapse-loop diagram.
3. Confirm write integrity on all three surfaces is preserved.
4. Size the work. If it's more than ~200 lines across files, scope down to the
   smallest slice that is still a working, testable unit. Do the slice. Leave
   the rest in BACKLOG with a "partial — continued from commit X" note.

---

## Step 4 — Build

Write the code. Do not describe it. Do not plan it out in prose.
Write the migration, the route, the template, the test — whatever the item needs.

One working unit per iteration. A working unit means:
- The feature does what the backlog item says it does
- It is tested (existing tests pass + new test if the item adds behavior)
- It has no syntax errors (run the file if unsure)

---

## Step 5 — Test

```bash
source .venv/bin/activate && python -m pytest tests/ -q --tb=short
```

All tests must pass before committing. If they fail, fix them — do not skip.
If a new test is needed, write it in `tests/`.

---

## Step 6 — Commit

```
feat: <what was built> — closes [Loop Node Name] node
```

Include the loop node name in the commit message so the history is traceable.

---

## Step 7 — Update BACKLOG.md

Mark the completed item done with: `— commit <hash> (date)`
If partial: update the item description with what remains, leave it in Active.
Add any newly discovered open nodes at the bottom of Active.

---

## Step 8 — Fire next iteration immediately

Use ScheduleWakeup:
- prompt: `<<autonomous-loop-dynamic>>`
- delaySeconds: 60
- reason: "Synapse build loop — completed [item just done], next: [next backlog item]"

The loop is the work. Keep it running.
