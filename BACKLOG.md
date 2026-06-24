# Synapse — Open Loop Nodes

Ranked by dependency order: each item unblocks the next.
The loop reads this file and takes the top unblocked item each iteration.

---

## Active

### 1. Thread status system
**Loop node:** Atom lifecycle → UI state map
**Why first:** Everything downstream (notifications, routing display, re-engagement)
depends on threads having a trackable status. Without this, notifications and
the gathering/answered/reopened states are impossible to wire.
**Scope:** DB migration + Python model + Flask route to update status
**Files to touch:**
- `db/migrations/` — add `thread_status` column to `memory_atoms` or new `threads` table
- `webapp/routes.py` — status update endpoint
- `webapp/templates/site/` — status badges on brain/discussion views
**Done when:** A memory atom can be in one of: active / gathering / updated / answered /
validated / reopened / unresolved. Status is readable from the UI. Transition
logic exists (at minimum: atom created → active, dual-write complete → updated).

---

### 2. Confidence gate — route vs. answer decision
**Loop node:** Confidence Evaluation (gate: answer / inject / iterate / route)
**Depends on:** None (chat.py already has direct/context/recursive routing — extend it)
**Scope:** Score the retrieval quality, decide whether to answer or route to forum
**Files to touch:**
- `app/chat.py` — add `route_to_forum` branch alongside direct/context/recursive
- `app/context_evaluator.py` — expose confidence score as numeric (not just label)
- `webapp/routes.py` — when gate fires, create a forum thread instead of answering directly
**Done when:** A question with no matching atoms AND low web-search confidence creates
a forum thread with status=gathering instead of returning a low-confidence answer.

---

### 3. Forum thread creation
**Loop node:** Low confidence → Generate routed forum thread
**Depends on:** Thread status system (#1), Confidence gate (#2)
**Scope:** When gate routes, create a `discussions` record with the original question,
target criteria, and status=gathering
**Files to touch:**
- `webapp/routes.py` — `/discussions/new` POST from confidence gate
- `db/` — ensure discussions table has: question_atom_id, target_criteria, status, created_by
- `webapp/templates/site/discussions.html` — show gathering threads
**Done when:** A routed question appears as a live discussion with status "Gathering perspectives".

---

### 4. Notification system — dual-write trigger
**Loop node:** Notification fires → User returns
**Depends on:** Thread status system (#1)
**Scope:** On dual-write complete, queue a notification for the originating user.
Tier: immediate for answered/reopened, batched for updated.
**Files to touch:**
- `db/migrations/` — `notifications` table (user_id, atom_id, type, read_at)
- `app/commit_pipeline.py` — emit notification after successful dual-write
- `webapp/routes.py` — `/notifications` route + unread count in nav
- `webapp/templates/site/base.html` — unread badge on nav
**Done when:** After a dual-write completes, an unread notification appears in the
nav for the user whose atom was updated.

---

### 5. User expertise signals
**Loop node:** Targeted users with matching signals respond
**Depends on:** Forum thread creation (#3)
**Scope:** Infer expertise from behavioral signals (categories engaged, questions answered,
atoms created). Used for routing — who gets notified about a gathering thread.
**Files to touch:**
- `db/migrations/` — `user_expertise` or signals on `users` table
- `app/` — expertise inference from signal history
- Routing logic in forum thread creation
**Done when:** A gathering thread notifies users whose atom history suggests
relevant expertise, not just all users.

---

### 6. MCP SSE transport (hosted users)
**Loop node:** Surface write integrity — three surfaces writing
**Depends on:** Nothing (auth already exists)
**Scope:** Enable `MCP_TRANSPORT=sse` mode so hosted users can connect Claude Desktop
or VS Code to the Synapse site without running Python locally.
Token auth middleware: reads Bearer token, injects source_user_id.
**Files to touch:**
- `mcp_server/server.py` — SSE transport branch (mostly already implemented)
- `webapp/routes.py` or separate entry — `/mcp/sse` dispatcher
- `webapp/templates/site/settings.html` — SSE config snippet
**Done when:** A user with an api_token can point Claude Desktop at the hosted
site's MCP SSE URL and store atoms under their identity.

---

### 7. Voyage AI embedding migration
**Loop node:** Migration-aware (anti-pattern: building on pre-migration embedding space)
**Depends on:** All above (do last before launch)
**Scope:** Swap `qwen3-embedding` (4096-dim) → Voyage AI `voyage-3` (1024-dim).
Re-embed all existing atoms. Enable HNSW index post-migration.
**Blocker:** Requires Voyage AI API key + cost decision.
**Done when:** All atoms use 1024-dim vectors. Cosine search returns correct results.
HNSW index enabled. Old qwen3 embeddings removed.

---

## Completed

- `source_user_id` propagation fix — commit `0318e19` (2026-06-24)
  Threaded `source_user_id` through `chat_with_research` → `_post_turn_reflection`
  → `run_turn_reflection` → `commit_candidate`. Brain page now shows atoms.
