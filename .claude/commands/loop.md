---
description: >
  Synapse autonomous build loop. Driven entirely by the spec in project-loop.md.
  Audits the codebase against the loop diagram each iteration, finds the next
  gap, closes it, and repeats. Stops at milestones for human review.
  Does not use a task list. Does not wait for user direction.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, mcp__memoryLayer__memory_store_auto, mcp__memoryLayer__memory_search
---

Your goal is to complete the Synapse project by closing every gap between
the current codebase and the spec. The spec is the authority. Not a task list.

---

## Step 1 — Load the spec

Read this file completely before touching any code:
/home/ricky/memory-layer/.claude/project-loop.md

This contains:
- The core loop diagram (what the system must do)
- The design philosophy (five questions — check every feature against all five)
- The anti-patterns (what must never be built)
- The milestones (where to stop for human review)

If you do not understand a section, read it again. Do not proceed until you
can answer: what does Synapse do that a forum + LLM layer cannot approximate?

---

## Step 2 — Audit the codebase against the spec

Do NOT read a task list. Derive the next gap yourself.

Read the core loop diagram in the spec. Then check the codebase:

```bash
git log --oneline -10          # what was recently built
git status                     # what is uncommitted
```

For each node in the loop diagram, ask:
> "Does working code exist for this node right now?
> Can I trace data flowing through it end-to-end?
> If I removed this code, would the loop break?"

The first node where the answer is no — or where the code exists but does not
honour the design philosophy — is the gap you work on this iteration.

---

## Step 3 — Check the five design questions before building

From the DESIGN PHILOSOPHY section of the spec:

1. Ask yourself: does this feature stall the user or make them feel like they
   are waiting for humans? If yes — redesign before building.

2. Ask yourself: does this feature expose the routing mechanism to the user?
   If yes — the pipe must be invisible. Redesign before building.

3. Ask yourself: does this feature surface raw human responses to the user?
   If yes — the AI must mediate. Redesign before building.

4. Ask yourself: does this feature break if no profiles exist?
   If yes — add a broadcast fallback before building the targeted version.

5. Ask yourself: does this feature grow the corpus or just the interface?
   If only the interface — reconsider whether it belongs in this iteration.

If any answer is wrong — acknowledge the misalignment. Correct the direction.
Then build.

---

## Step 4 — Build

Write the code. Do not describe it. Do not plan it in prose. Build it.

Target: one working unit per iteration. A working unit is the smallest thing
that closes a gap in the loop — a route, a migration, a wired UI event, a test.

If the gap is too large for one iteration: build the first logical slice,
commit it, and continue from that slice next iteration.

---

## Step 5 — Test

```bash
source .venv/bin/activate && python -m pytest tests/ -q --tb=short
```

All tests must pass. If they fail — fix them. If the item adds behaviour — write a test.

---

## Step 6 — Commit

```
feat: <what was built> — closes [loop node name]
```

---

## Step 7 — Check for milestone completion

Read the MILESTONES section of the spec. Run through each milestone's check
questions against the live codebase.

Ask yourself:
> "Can I demonstrate this milestone end-to-end right now on the live site,
> without explaining what it's supposed to do?"

**If a milestone is now complete:**
1. Write a milestone report to MILESTONE_LOG.md:
   - Which milestone was reached
   - What was built to get here
   - What each of the milestone's check questions now answers
   - Any gaps found and corrected along the way
   - What the next milestone requires
2. Do NOT call ScheduleWakeup.
3. Stop. Wait for the user to review and resume with /loop.

**If no milestone is complete:**
Call ScheduleWakeup:
- prompt: `<<autonomous-loop-dynamic>>`
- delaySeconds: 60
- reason: "Synapse build loop — completed [what you built], next gap: [what you found]"
