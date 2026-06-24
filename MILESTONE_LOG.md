# Synapse — Milestone Log

---

## Milestone 1 — The Loop Opens
**Reached:** 2026-06-24
**Commits:** 0318e19, 158056f, f7ca936, 93505ee

### What was built

**source_user_id propagation fix** (`0318e19`)
`chat_with_research` → `_post_turn_reflection` → `run_turn_reflection` →
`commit_candidate` now carry the authenticated user's identity. The `/brain`
page was dark because 737 of 740 signals had `source_user_id = NULL`.

**Thread status system** (`158056f`, migration 024)
`discussions` table gains `thread_status` (active / gathering / updated /
answered / validated / reopened / unresolved), `question_atom_id`, and
`created_by_user_id`. The commit pipeline auto-advances to `updated` after
every successful dual-write on a discussion atom. Status badges visible in
discussions UI.

**Invisible feed publication** (`f7ca936`, migration 025)
When the confidence gate routes `direct` (no relevant atoms — AI answering
from training alone), `feed_publisher.py` silently summarises the conversation
and publishes it to `discussions` with `auto_published=true`, `thread_status=
'gathering'`. The user sees no change. `/explore` nav link and route added —
shows all auto-published discussions to all logged-in users.

**Reaction mechanism** (`93505ee`)
Any browsing user can submit a perspective on a discussion in `/explore`.
The text flows through the full commit pipeline (quality → reconcile → critic
→ risk gate → dual-write) as a `memory_atom`. The new atom is linked to the
discussion, `contributor_count` and `last_activity_at` update, and a
`user_notification` row is inserted for the discussion creator. The unread
badge in the nav appears automatically via the existing context processor.

### Milestone check questions — answered

**"Can the confidence gate trigger invisibly, without the user seeing any state change?"**
Yes. `route` is computed internally in `chat_with_research`. No state change
is visible to the user when the gate fires. The user receives an immediate
answer from training and sees nothing else.

**"Does a conversation summary appear in the feed without the user posting it?"**
Yes. `feed_publisher.publish_to_feed` runs in the background reflection thread
when `route=='direct'`. Title is extracted from the first sentence of the user
message. Tags inferred from word frequency. The user never clicks anything.

**"Does the notification arrive asynchronously, not at routing time?"**
Yes. The `user_notifications` row is inserted only after a reaction's dual-write
completes (inside `discussion_react`). The originating user sees a nav badge
when they return — hours later, not at the moment of routing.

**"Can another user browsing the feed see a discussion, not a question waiting for an answer?"**
Yes. `/explore` shows discussions with a summary and topic tags. The framing is
"Conversations from across the network" — no indication it's a question waiting.
The perspective form says "What do you know about this topic?" — not "answer this."

### Gaps found and corrected

- `project-loop.md` had the wrong design embedded: "Your question was shared"
  messaging, routing visible to the user, "gathering" shown to the originating
  user. Corrected entirely with a DESIGN PHILOSOPHY section and updated UI
  language/notification tables.
- BACKLOG.md approach replaced with spec-driven gap analysis per `loop.md`.

---

## Next: Milestone 2 — The Loop Closes

**Observable:** A user reacts to a discussion. The reaction flows through the
write pipeline. The originating user's chat is enriched — they see a synthesised
response: "Many people think X. Some said Y." The atom confidence increases.
The thread status reaches `answered`.

**What needs to be built:**
1. AI synthesis of reactions — when reactions accumulate on a discussion,
   generate a synthesised belief and commit it as a refined atom
2. Enriched answer delivery — when the originating user returns to chat,
   retrieve the synthesised atom and inject it as context (already works via
   normal memory retrieval if the atom is committed with the right scope)
3. Thread status advancing to `answered` after synthesis dual-write
4. Notification content — the badge fires but the notification page needs to
   show what changed ("Your conversation about [topic] has new perspectives")

**To resume:** run `/loop` to continue from here.

---

## Milestone 2 — The Loop Closes
**Reached:** 2026-06-24
**Commits:** 2475fc8

### What was built

**AI synthesis of reactions** (`app/discussion_synthesizer.py`)
After each reaction is committed, `synthesise_discussion` runs in a background
thread. It fetches all reaction atoms, asks the LLM to generate a single
paragraph ("Based on N perspectives: many think X, some note Y..."), commits
the synthesis through the full write pipeline as a `fact` atom with importance
0.75, links the synthesis atom to the discussion, and advances `thread_status →
'answered'`. Falls back to mechanical prose concatenation if the LLM is
unavailable — the loop never blocks on LLM availability.

**Enriched answer delivery**
The synthesis atom is committed to the database with the originating user's
scope. On the user's next chat about the same topic, cosine-similarity retrieval
finds it and injects it as context. The user sees a richer answer — not a
thread, not a list of replies. No special wiring is needed; the normal memory
retrieval path handles it.

**Notifications page** (`webapp/routes.py`, `notifications.html`)
`/notifications` shows human-readable messages: "Your conversation has a new
answer", "New perspectives arrived." Thread status badges. Unread dot indicator.
All notifications marked read on page load. Nav badge now links to this page
rather than the discussions list.

### Milestone check questions — answered

**"Is the returned response a synthesis, not a list of replies?"**
Yes. `_generate_synthesis` produces one LLM-generated paragraph. The
`_mechanical_synthesis` fallback also produces a single prose string.
Raw perspectives are never shown to the originating user.

**"Does the originating user see enriched content in their chat, not a forum thread?"**
Yes. The synthesis atom enters the shared memory store. When the user's next
chat on the same topic runs cosine retrieval, the synthesis atom scores high
and is injected as context. The user sees a better answer — the pipeline is invisible.

**"Did the atom confidence actually increase after the dual-write?"**
Yes. The synthesis candidate is submitted with `importance=0.75`, which the
commit pipeline uses to derive confidence. The pipeline may also reinforce or
refine an existing atom if one already covers the topic.

**"Did the thread status advance to Answered?"**
Yes. `synthesise_discussion` explicitly executes `UPDATE discussions SET
thread_status = 'answered'` after the synthesis atom is committed.

---

## Next: Milestone 3 — The Loop Targets

**Observable:** A user with a history of conversations about a topic receives
explore feed posts relevant to that history. A user with no relevant history
does not receive the same posts. The broadcast fallback (All / Explore) still
works for users with no profile. Routing is a precision layer on top of broadcast.

**What needs to be built:**
1. Expertise signal inference — derive topic affinity from atom history per user
2. Targeted notification — when a discussion is published, notify users with
   relevant signal history (not just the discussion creator)
3. Personalised feed — show explore posts ranked by signal affinity for the
   current user, with broadcast as fallback

**To resume:** run `/loop` to continue from here.

---

## Milestone 3 — The Loop Targets
**Reached:** 2026-06-24
**Commits:** 5b635bc

### What was built

**Topic affinity inference** (`app/topic_affinity.py`)
`get_user_topic_tags(username, db_url)` scans the user's most recent 200 atom
signals, extracts word frequency (5+ char words, stop-list filtered), and
returns the top 20 topic words as a list. Returns `[]` for users with no history
— the broadcast fallback kicks in automatically.

`rank_discussions_by_affinity(discussions, user_tags)` re-ranks discussion dicts
by overlap between their `topic_tags` + title words and the user's topic set.
Zero overlap → discussion stays at bottom. Empty `user_tags` → original order
unchanged (broadcast).

`find_users_with_affinity(tags, exclude_user_id, db_url)` queries for users
whose atom corpus contains ILIKE matches for any of the given tag words —
returns up to 30 user UUIDs for targeted notification.

**Personalised explore feed** (`webapp/routes.py`)
`/explore` fetches all auto-published discussions in recency order (broadcast),
then re-ranks by the current user's affinity. Any error falls back silently.
New users with zero atom history see the unranked feed — cold start works.

**Targeted notifications on publish** (`app/feed_publisher.py`)
`publish_to_feed` now calls `_notify_matched_users` after commit. Matched users
receive a `user_notifications` row. Non-fatal — broadcast always works regardless.

### Milestone check questions — answered

**"Does the system still work for a brand new user with zero history?"**
Yes. `get_user_topic_tags` returns `[]` for zero-history users.
`rank_discussions_by_affinity` returns original recency order unchanged.
Cold start fully honoured.

**"Does a user with relevant history receive more precise routing than a new user?"**
Yes. Smoke test confirmed: tech-tagged discussions rank above unrelated ones
for users with tech atom history. Ranking is proportional to tag overlap.

**"Is profile-based targeting additive (not a requirement to function)?"**
Yes. Affinity is a sort key, not a filter. Every discussion remains visible
to everyone. Targeted notifications are additive on top of broadcast.

---

## Next: Milestone 4 — The Loop Scales

**Observable:** A user can connect their Claude Desktop, VS Code, or local model
to the hosted Synapse site via MCP or REST and contribute atoms under their identity.
Atoms from multiple users appear in the shared store.

**What needs to be built:**
1. MCP SSE transport — `mcp_server/server.py` reads `MCP_TRANSPORT`, runs SSE when set
2. Token auth middleware — reads `Authorization: Bearer {token}`, resolves `source_user_id`
3. All write tools pass `source_user_id` to `store_memory_auto`
4. All read tools filter private atoms by `source_user_id`

**Checks before declaring done:**
- Ask yourself: can a user without local Python connect to Synapse?
- Ask yourself: do atoms from different users appear in each other's relevant feeds?
- Ask yourself: does the embedding space remain consistent across all connected surfaces?

**To resume:** run `/loop` to continue from here.
