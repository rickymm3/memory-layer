---
description: >
  Synapse master development loop. Invoke at the start of every session.
  Checks every implementation decision against the full Synapse loop —
  memory write integrity, atom lifecycle, UI surface wiring, confidence
  gate, routing logic, notification architecture, and session-close
  resolution. If a feature cannot be traced to a node in this loop,
  it does not belong in Synapse.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# SYNAPSE — MASTER DEVELOPMENT LOOP
> You are working on Synapse: a persistent memory layer for LLMs that turns
> normal conversations into a collectively-refined knowledge network — invisibly,
> without disrupting the user's chat experience.
>
> Before every implementation step, run this loop.
> If the work connects to a node — continue.
> If it does not — acknowledge the gap, correct the direction, then continue.

---

## THE CORE LOOP

Every feature, every page, every tool, every notification must trace back
to a node in this chain. If it does not — it is not Synapse. It is generic software.

```
USER TALKS TO THEIR AI (normally — no change in experience)
  └─ Memory Retrieval
       (cosine search → pgvector → atom scoring → ranked context)
       └─ Confidence Evaluation
            (gate: answer / inject / iterate / route)
            │
            ├─ [HIGH CONFIDENCE]
            │    Inject atoms → Deliver answer → Reflect → Commit
            │
            └─ [LOW CONFIDENCE]
                 AI answers immediately from what it knows (never stalls the user)
                 └─ Conversation is summarised → published as a shareable post
                      (user does not post — the AI does it invisibly)
                      └─ Post appears in feeds: All / Popular / Categories
                           (other users browse and react — they don't know they're
                            answering a question; it looks like a discussion post)
                           └─ Reactions collected and weighted by:
                                 · recency of claimed expertise
                                 · behavioral signal strength
                                 · agreement / disagreement across responders
                                 · evidence quality
                                 · prior usefulness outcomes
                              └─ AI synthesises weighted reactions into enriched belief
                                   ("Many people think X, some say Y,
                                    those close to the subject say Z")
                                   └─ Write pipeline:
                                         quality score
                                         → reconcile
                                         → critic LLM review
                                         → risk gate
                                         → dual-write (atom + signal)
                                      └─ Atom confidence updates
                                           └─ HOURS LATER: notification fires
                                                ("New perspectives on your conversation
                                                 about [topic]")
                                                └─ User returns to their chat
                                                     └─ Sees enriched answer, not a thread
                                                          └─ Loop repeats, stronger
```

---

## DESIGN PHILOSOPHY — READ THIS BEFORE BUILDING ANYTHING

These are the principles that separate Synapse from "Reddit + an LLM layer."
Ask yourself before every feature: does this honour all five?

**1. The user never waits for humans.**
The AI always answers immediately from what it knows. Enrichment from human
reactions arrives asynchronously — hours later as a notification, not a spinner.
Ask yourself: does this feature stall the user or make them feel like they're
waiting? If yes, it is wrong. Acknowledge, redesign, then continue.

**2. The user never sees a forum.**
Users talk to their AI. They do not post, reply, or browse threads.
The AI summarises their conversation and publishes it invisibly.
Other users browse a feed and react — but those users experience it as exploring
discussions, not answering someone's question.
Ask yourself: does this feature expose the routing mechanism to the user?
If yes, it is wrong. The pipe must be invisible.

**3. The returned insight is a synthesis, not a thread.**
When enrichment arrives, the user sees one AI-generated response:
"Many people think X. Some said Y. Those closest to the subject say Z."
They do not read replies. They do not see usernames. They receive a belief update.
Ask yourself: does this feature surface raw human responses to the user?
If yes, it is wrong. The AI mediates everything.

**4. Cold start is solved by broad feeds, not profiles.**
On day one, with zero profile data, posts go to All / Popular / Categories.
Anyone can browse and react. Profile-based targeting is a precision layer added
on top — it narrows routing over time, but is never required for the system to work.
Ask yourself: does this feature break if no profiles exist?
If yes, it needs a broadcast fallback before it can ship.

**5. Stickiness comes from the corpus, not the community.**
The more conversations flow through Synapse, the smarter every connected LLM gets.
A local model connected to Synapse gains access to validated, weighted beliefs
it could never derive from training alone. That compounding value — not social
features — is why users stay and why the system improves with scale.
Ask yourself: does this feature grow the corpus or just the interface?
If only the interface — it is decoration. If it grows the corpus — it belongs.

---

## SURFACE WRITE INTEGRITY — CRITICAL SYSTEM CHECK

The memory layer only exists if every surface is writing to it.
Synapse has three active write surfaces:

```
  · Claude Desktop    (MCP over WSL stdio)
  · Claude CLI        (MCP via settings hooks)
  · Web App / Flask   (POST /api/ingest + POST /mcp/sse)
```

All three must write to the same PostgreSQL store.
If any surface silently stops writing → the memory layer degrades
without any visible error, without any alert, and without the loop
detecting it. The product fails quietly. That is the worst failure mode.

---

BEFORE CHANGING ANY TOOL, PROCESS, OR INTEGRATION — ASK:

**1. Does this change touch the write path of any surface?**
(MCP tool update, endpoint change, auth change,
embedding model swap, schema migration, hosting change)
If yes → treat this as a write-integrity event.
Do not ship until write continuity is confirmed on all three surfaces.

**2. After the change — can you prove all three surfaces are still writing?**
Run `memory_health`.
Run a test atom write from each surface independently.
Confirm the atom appears in the shared store.
If any surface fails to confirm → the change is not complete.
Roll back or fix before proceeding.

**3. Is this change embedding-aware?**
If the embedding model or dimensions change (e.g. qwen3 4096 → Voyage 1024),
atoms written before the migration are in a different vector space.
Cross-surface reads will silently return wrong results.
Flag it. Coordinate the migration across all surfaces simultaneously.
Never let surfaces write in mixed embedding spaces.

**4. Could this change cause a silent write failure?**
Silent failures are the primary risk. Ask:
- Does the surface report success but skip the DB write?
- Does the MCP tool return without confirming dual-write?
- Does the Flask endpoint accept the payload but fail at ingest?

If you cannot rule these out → add a write confirmation log before shipping.

---

WRITE INTEGRITY RULE:

No tool change, process update, integration refactor,
or infrastructure move is complete until:

```
  ✓ memory_health returns green on all surfaces
  ✓ A test atom written from each surface is readable from all others
  ✓ The dual-write log confirms atom + signal were both committed
  ✓ No surface is writing in a different embedding space than the others
```

If any of these four checks fail → the loop does not run.
Everything built on top of a broken write is built on nothing.
Acknowledge. Fix the write. Then continue.

---

## BEFORE EVERY IMPLEMENTATION DECISION — ASK:

### 1. Does this feature connect to a named node in the loop?
If you cannot point to the exact node it reads from or writes to →
it is premature, misaligned, or generic social media.
**Acknowledge. Stop. Redirect.**

### 2. Does this preserve full traceability?
Can you trace from:
```
original question
  → confidence gate decision
    → routed thread (if applicable)
      → responder relevance signal
        → human claims made
          → evidence provided
            → memory atom created
              → belief update applied
                → improved answer delivered
                  → user notified
```
If any link is broken or unlogged → the loop cannot self-improve.
**Fix the gap before building the next node.**

### 3. Does this work with a sparse profile?
The system must function on day one with zero profile data.
Routing falls back to broad broadcast — All / Popular / Categories.
Ask: would a user with no history still get value from this?
If no → it is profile-dependent and needs a fallback path.

### 4. Does this get stronger with a rich profile?
Expertise must be inferred from behavior:
- categories engaged with
- questions answered and rated useful
- conversations initiated
- geographic or temporal context surfaced in atoms
- usefulness outcomes of prior contributions

Ask: does this feature extract or consume those signals?
If neither → it is passive and will not compound over time.

### 5. Does this thread have a status the system can close?
Every thread must be exactly one of:

| Status | Meaning | System Action Available |
|---|---|---|
| Active | AI is processing | None |
| Gathering | Routed, awaiting responses | Monitor for signals |
| Updated | Atom written, user not yet notified | Fire notification |
| Answered | Confident atom committed | Deliver enriched response |
| Validated | Post-hoc usefulness confirmed | Increase atom confidence |
| Reopened | New evidence arrived | Re-engage user |
| Unresolved | No strong answer yet | Keep routing |

If a thread has no closeable status → it is a dead post, not a memory event.
**Do not build dead posts.**

### 6. Is this feature migration-aware?
The embedding migration from qwen3 (4096-dim) to Voyage AI voyage-3
(1024-dim, HNSW) is a live blocker.
Ask: are we building on top of atoms that will need to be re-embedded?
If yes → flag it. Do not architect permanent features on the old embedding space.
**Build migration-aware at every step.**

### 7. Does this help the confidence gate improve over time?
The gate is the heart of the system.
Ask: does this feature give the gate better signal for deciding
when to answer vs. when to route?
If the gate is not improving → the AI stays static and the forum exists for no reason.

---

## ATOM LIFECYCLE → UI STATE MAP

The UI is not a skin on top of Synapse.
The UI is the user-facing surface of the memory loop.
Every internal event that changes an atom's state must produce a visible
change in the interface.
**If an atom is touched and the user doesn't see it → the loop is broken
at the surface. Wire the event. Then continue.**

| Atom Event | UI Response |
|---|---|
| Atom created from conversation | Conversation marked **Active**. No user-facing message. |
| Cosine search retrieves atom | No UI change (internal only) |
| Confidence gate: answer directly | Response delivered immediately. No routing. |
| Confidence gate: route | AI answers from current knowledge. Conversation summarised and published to feeds invisibly. **No UI change for the user — they do not know this happened.** |
| Other user reacts via feed | Signal logged. Synthesis queued. No UI change yet. |
| Quality score updated | No UI change (internal only) |
| Reconciliation pass runs | No UI change (internal only) |
| Critic LLM review completes | No UI change (internal only) |
| Risk gate passed | No UI change (internal only) |
| **Dual-write complete** ← trigger point | Status → **Updated**. Notification fires: *"New perspectives on your conversation about [topic]."* |
| Confidence score increases | Status → **Answered**. User returns to chat → sees enriched AI synthesis, not a thread. |
| Post-hoc usefulness confirmed | Status → **Validated**. Atom confidence increases again. |
| New evidence arrives | Status → **Reopened**. Notification fires: *"Your conversation about [topic] has new information."* |
| Atom flagged for revision | Conversation shows revision badge. User sees: *"This answer may have changed."* |

> **Rule:** Internal-only events do not fire UI changes.
> Dual-write completion is the trigger point.
> Everything before that is pipeline. Everything after that is surface.
> The user must never feel like they are waiting for humans.

---

## UI LANGUAGE TRANSLATION LAYER

Users must never encounter system vocabulary.
Every internal term has a user-facing equivalent.
**If a user needs to understand what happens inside the pipe to use the
product → the translation layer has failed. Rewrite the copy. Then continue.**

| Internal Term | User-Facing Language |
|---|---|
| Memory atom | Your conversation / discussion |
| Confidence gate triggered | Not surfaced — internal only. User sees no change. |
| Routing to feeds | Not surfaced — internal only. User sees no change. |
| Belief weighting | Not surfaced — internal only |
| Embedding similarity | Not surfaced — internal only |
| Critic LLM review | Not surfaced — internal only |
| Confidence score | Answer quality indicator (optional, subtle) |
| Atom reconciliation | Not surfaced — internal only |
| Risk gate | Not surfaced — internal only |
| AI synthesis of reactions | *"New perspectives on your conversation about [topic]"* |
| Usefulness rating | *"Was this helpful?"* |
| Atom reopened | *"Your conversation about [topic] has new information"* |
| Expert signal match | Not surfaced — internal only |

---

## NOTIFICATION ARCHITECTURE

Notifications are the re-engagement mechanism.
Without them the loop closes but the user never returns.

**Before shipping any notification, ask:**
1. What atom event fired this? *(If you cannot name the event → do not send it)*
2. What does the user need to do with this? *(If "nothing" → suppress or batch it)*
3. Is this the right time? *(Batch minor. Fire immediately for Answered / Reopened)*

### Notification Tiers

**IMMEDIATE** — fire on event:
- Conversation status changed to **Answered** (enriched synthesis ready)
- Conversation status changed to **Reopened** (new evidence arrived)

**BATCHED** — digest, max once per session or per day:
- Conversation status changed to **Updated** (synthesis in progress, not yet complete)
- A discussion you reacted to received further updates

**SILENT** — logged, never pushed:
- Confidence gate triggered (user must not know routing happened)
- Conversation published to feeds (user must not know this happened)
- Reconciliation pass ran
- Quality score changed
- Embedding re-indexed

> **Rule:** Notification fatigue kills re-engagement.
> If every atom touch fires a notification → users will mute Synapse within a week.
> Only surface-level state changes notify. Only dual-write completion triggers a push.

---

## PRIMARY NAVIGATION — NON-NEGOTIABLE

| Tab | What It Answers |
|---|---|
| **Home** | What is new since I was last here? |
| **Discussions** | All conversations, filterable by status |
| **Categories** | Browsable by topic — no profile required |
| **Search** | Find before posting — reduce duplicates |
| **Notifications** | What changed on conversations I care about? |

> **Rule:** Navigation must never change based on internal state.
> A user with no profile sees the same nav as a power user.
> Personalization lives in the feed — not in the structure.

---

## EVERY PAGE MUST ANSWER THREE QUESTIONS

Before shipping any page or component, confirm it answers:

1. **What happened?** *(What changed since you were last here?)*
2. **What should I do next?** *(Is there an action available — rate, respond, follow up?)*
3. **Why am I seeing this?** *(Did I ask this? Did I respond? Did it match something I care about?)*

If a page cannot answer all three → it is incomplete for the user even if
the data is correct.
**Acknowledge. Add the missing context. Then continue.**

---

## ANTI-PATTERNS THAT KILL THE LOOP

```
✗  Building forum UI that doesn't write to memory atoms
✗  Building memory atoms with no traceability to their source question
✗  Building routing before the confidence gate exists
✗  Building profiles as the only path to relevance (sparse users break — feeds first)
✗  Building notifications without a thread status system
✗  Building synthesis without a trust/audit path
✗  Building for engagement metrics instead of resolution metrics
✗  Building any feature that makes the system more clever but harder to understand
✗  Shipping a thread with no closeable status
✗  Firing a notification you cannot trace to a named atom event
✗  Using internal vocabulary in user-facing copy
✗  Building permanent features on the pre-migration embedding space
✗  Changing any MCP tool, endpoint, or integration without running write integrity checks
✗  Assuming a surface is still writing because it was writing before the change
✗  Letting surfaces write in mixed embedding spaces during or after migration
✗  Treating a silent write failure as a passing test
✗  Showing the user a "gathering" or "waiting" state — the pipe is always invisible
✗  Surfacing raw human responses — the AI always mediates and synthesises
✗  Building direct human-to-human interaction paths — Synapse is AI-mediated only
✗  Requiring profiles before the system can route — broadcast feeds are always the fallback
✗  Building a UX that makes the user feel like they posted on a forum
```

---

## UI LOOP CHECK — run before shipping any interface component

Ask yourself:

> *"If the atom pipeline runs right now and updates this conversation —
> would the user see something change on this page, without refreshing,
> without knowing what an atom is, and without feeling confused about
> why it changed?"*

If yes → the UI is correctly wired to the loop. Ship it.
If no → find the missing event binding. Name it. Wire it. Then ship.

**The interface is not done when it looks right.
The interface is done when every atom lifecycle event
produces the correct, plain-language, user-visible response.**

---

## SESSION-CLOSE RESOLUTION CHECK — run at the end of every build session

Ask yourself:

> *"Has every feature built today closed a gap in the loop, or opened one?"*

**If it closed a gap:**
Name which node it connects.
Confirm write integrity was not broken.
Commit it. Move to the next node.

**If it opened one:**
Name the gap explicitly.
Do not ship past an open loop node.
Acknowledge. Correct. Then continue.

---

## THE NORTH STAR

> Synapse is an AI that knows when it does not know —
> creates the right conversation,
> routes it to the right people,
> learns from the responses,
> and gives users a clean place to track, resume, and benefit from that loop.
>
> The best version of Synapse feels obvious to use
> while hiding every complexity underneath.
>
> **Resolution rate — not engagement rate — is the metric that tells you it's working.**
> The loop succeeds when the percentage of routed questions
> that return an improved, committed atom goes up.
> That number is the product.

---

## MILESTONES — WHERE THE LOOP STOPS FOR HUMAN REVIEW

These are observable states, not task checklists.
At each milestone: stop the loop, write a milestone report to MILESTONE_LOG.md,
do not schedule the next iteration. Wait for the user to review and resume.

Ask yourself at each milestone check:
> *"Can I demonstrate this capability end-to-end right now, on the live site,
> without describing what it's supposed to do? If I have to explain it,
> it is not done."*

---

### MILESTONE 1 — The Loop Opens
**Observable:** A user has a conversation. Hours later, that conversation appears
as a post in the Synapse feed. Another user can browse it and react.
The originating user receives a notification — not immediately, but later.
The user who originally chatted never sees a forum. They see: "New perspectives on your conversation."

**Checks before declaring done:**
- Ask yourself: can the confidence gate trigger invisibly, without the user seeing any state change?
- Ask yourself: does a conversation summary appear in the feed without the user posting it?
- Ask yourself: does the notification arrive asynchronously, not at routing time?
- Ask yourself: does another user browsing the feed see a discussion, not a question waiting for an answer?

If any check is no → it is not done. Acknowledge. Fix. Then declare.

---

### MILESTONE 2 — The Loop Closes
**Observable:** A user reacts to a discussion in the feed. That reaction flows
through the write pipeline. The originating user's chat is enriched — they see
a synthesised response: "Many people think X. Some said Y." The atom's confidence
increases. The thread status reaches Answered.

**Checks before declaring done:**
- Ask yourself: is the returned response a synthesis, not a list of replies?
- Ask yourself: does the originating user see enriched content in their chat, not a forum thread?
- Ask yourself: did the atom confidence actually increase after the dual-write?
- Ask yourself: did the thread status advance to Answered?

If any check is no → it is not done. Acknowledge. Fix. Then declare.

---

### MILESTONE 3 — The Loop Targets
**Observable:** A user with a history of conversations about a topic receives feed
posts relevant to that history. A user with no relevant history does not receive
the same posts. The broadcast fallback (All/Popular) still works for users with
no profile. Routing is a precision layer on top of broadcast, not a replacement.

**Checks before declaring done:**
- Ask yourself: does the system still work for a brand new user with zero history?
- Ask yourself: does a user with relevant history receive more precise routing than a new user?
- Ask yourself: is profile-based targeting additive (not a requirement to function)?

If any check is no → it is not done. Acknowledge. Fix. Then declare.

---

### MILESTONE 4 — The Loop Scales
**Observable:** A user can connect their Claude Desktop, VS Code, or local model
to the hosted Synapse site via MCP or REST and contribute atoms under their identity.
Atoms from multiple users appear in the shared store. Semantic search returns
correct results across all users' atoms.

**Checks before declaring done:**
- Ask yourself: can a user without local Python connect to Synapse?
- Ask yourself: do atoms from different users appear in each other's relevant feeds?
- Ask yourself: does the embedding space remain consistent across all connected surfaces?

If any check is no → it is not done. Acknowledge. Fix. Then declare.
