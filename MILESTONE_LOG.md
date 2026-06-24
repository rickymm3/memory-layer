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
