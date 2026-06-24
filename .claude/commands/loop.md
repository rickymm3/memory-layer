---
description: >
  Synapse autonomous development loop. Reads the synapse-loop primer on every
  iteration, applies its checks to current work, continues implementation, and
  schedules the next iteration.
allowed-tools: Read, Write, Edit, Bash, mcp__memoryLayer__memory_task_context, mcp__memoryLayer__memory_store_auto, mcp__memoryLayer__memory_search
---

Start of loop iteration. Do the following steps in order:

**Step 1 — Load the primer.**
Read the file at:
/home/ricky/memory-layer/.claude/commands/synapse-loop.md

This is your operating framework for this session. Apply it to every decision below.

**Step 2 — Orient.**
- What is the current state of the project? Check git status and recent commits.
- What was the last thing worked on? Check task list if one exists.
- Is there anything broken or incomplete from the previous iteration?

**Step 3 — Apply the loop checks from the primer.**
Before touching any code, run through:
1. Does the next piece of work connect to a named node in the core loop?
2. Will write integrity be preserved across all three surfaces?
3. Does it handle the sparse-profile case?

If any check fails — name the gap, correct direction, then continue.

**Step 4 — Do the work.**
Implement the next logical step. Prefer closing open loop nodes over starting new ones.

**Step 5 — Session-close resolution check.**
After work is done:
- Name which node was closed.
- Confirm write integrity was not broken.
- Name any gap that was opened (if any).

**Step 6 — Schedule next iteration.**
Use ScheduleWakeup with:
- prompt: `<<autonomous-loop-dynamic>>`
- delaySeconds: 1800
- reason: "Synapse development loop — checking for next open node"
