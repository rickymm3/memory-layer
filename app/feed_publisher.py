"""Invisible background publication of low-confidence conversations to the feed.

When the confidence gate routes a turn as 'direct' (no relevant atoms found,
AI answering from training alone), the conversation is summarised and published
as a discussion post. The user sees nothing — the pipe is fully invisible.
Other users browse the explore feed and react. Their reactions are later
synthesised back into an enriched belief for the originating user.
"""
from __future__ import annotations

import logging
import os
import re

_logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 100
_NOVELTY_GATE = 0.55  # minimum novelty_score to warrant a post; interest_flag always passes


def _make_title(user_msg: str) -> str:
    """Extract a short, readable title from the user message."""
    # Use first sentence, or first N chars if no sentence boundary
    text = user_msg.strip()
    m = re.search(r"[.!?]", text)
    if m and m.start() > 10:
        title = text[: m.start()]
    else:
        title = text
    title = " ".join(title.split())  # normalise whitespace
    if len(title) > _MAX_TITLE_LEN:
        title = title[: _MAX_TITLE_LEN - 1].rsplit(" ", 1)[0] + "…"
    return title or "Untitled discussion"


def _extract_tags(user_msg: str, answer: str) -> list[str]:
    """Simple keyword extraction for topic tags — no LLM needed."""
    combined = f"{user_msg} {answer}".lower()
    # Strip punctuation, split, keep words 5+ chars, dedupe, take top 5
    words = re.findall(r"[a-z]{5,}", combined)
    stop = {
        "about", "after", "again", "because", "being", "could",
        "their", "there", "these", "thing", "think", "those",
        "through", "using", "where", "which", "while", "would",
        "should", "other", "people", "really", "right", "since",
        "something", "still", "things", "though", "very",
    }
    seen: dict[str, int] = {}
    for w in words:
        if w not in stop:
            seen[w] = seen.get(w, 0) + 1
    ranked = sorted(seen, key=lambda w: -seen[w])
    return ranked[:5]


def _craft_post_body(user_msg: str, answer: str, tags: list[str]) -> str:
    """Ask the LLM to write an engaging discussion opener from the conversation.

    The post should read as an interesting stand-alone question or observation —
    not a raw AI answer, not a user message verbatim. Falls back to a cleaned
    excerpt if the LLM is unavailable.
    """
    prompt = (
        f"A person asked: {user_msg.strip()[:400]}\n\n"
        f"The AI's best answer was: {answer.strip()[:600]}\n\n"
        f"Topic tags: {', '.join(tags)}\n\n"
        "Write a short, engaging discussion post (2-3 sentences max) that:\n"
        "- Frames the topic as an interesting open question worth exploring\n"
        "- Does NOT start with 'I' or 'The AI'\n"
        "- Sounds like a curious person sharing something they're thinking about\n"
        "- Ends in a way that invites a response\n"
        "Return only the post body — no title, no label, no explanation."
    )
    try:
        from app.llm_provider import get_chat_client
        llm = get_chat_client()
        result = llm.generate_response(
            prompt,
            system="You write engaging, human-sounding discussion starters. Be concise and curious.",
        )
        if result and len(result.strip()) > 20:
            return result.strip()[:600]
    except Exception:
        pass
    # Fallback: first 2 sentences of answer, cleaned up
    cleaned = answer.strip()
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    return " ".join(sentences[:2])[:500] if sentences else cleaned[:500]


def _worth_surfacing(atom_ids: list[str], db_url: str) -> bool:
    """Return True if any atom clears the novelty gate.

    Checks interest_flag=true OR novelty_score >= _NOVELTY_GATE.
    Defaults to False on any DB failure so the gate is conservative.
    """
    if not atom_ids or not db_url:
        return False
    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT interest_flag, novelty_score
                    FROM memory_atoms
                    WHERE id = ANY(%s::uuid[]);
                    """,
                    (atom_ids,),
                )
                for row in cur.fetchall():
                    interest, novelty = row[0], float(row[1] or 0.0)
                    if interest or novelty >= _NOVELTY_GATE:
                        return True
    except Exception:
        pass
    return False


def publish_to_feed(
    user_msg: str,
    answer: str,
    committed_atom_ids: list[str],
    source_user_id: str | None,
    db_url: str | None = None,
) -> str | None:
    """Publish a conversation as a discussion post to the explore feed.

    Called invisibly in the background when route=='direct'. Returns
    the discussion UUID on success, None on any failure (non-fatal).
    """
    if not db_url:
        db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        return None

    title = _make_title(user_msg)
    tags = _extract_tags(user_msg, answer)
    summary = _craft_post_body(user_msg, answer, tags)

    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                # Resolve user UUID from username (source_user_id is the username string)
                user_uuid = None
                if source_user_id:
                    cur.execute(
                        "SELECT id FROM users WHERE username = %s;",
                        (source_user_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        user_uuid = row[0]

                # Create the discussion
                cur.execute(
                    """
                    INSERT INTO discussions
                        (title, topic_tags, seed_atom_ids, contributor_count,
                         atom_count, thread_status, auto_published, summary,
                         created_by_user_id)
                    VALUES (%s, %s, %s, 1, %s, 'gathering', true, %s, %s)
                    RETURNING id;
                    """,
                    (
                        title,
                        tags,
                        committed_atom_ids,
                        len(committed_atom_ids),
                        summary,
                        user_uuid,
                    ),
                )
                disc_id = cur.fetchone()[0]

                # Link committed atoms to the discussion
                for atom_id in committed_atom_ids:
                    try:
                        cur.execute(
                            """
                            INSERT INTO discussion_atoms
                                (discussion_id, atom_id, source_user_id, novelty_score)
                            VALUES (%s, %s, %s, 0.5)
                            ON CONFLICT (discussion_id, atom_id) DO NOTHING;
                            """,
                            (str(disc_id), str(atom_id), source_user_id or "anonymous"),
                        )
                    except Exception:
                        pass  # individual atom link failure is non-fatal

                # Notify the originating user that their conversation was shared
                if user_uuid:
                    try:
                        cur.execute(
                            """
                            INSERT INTO user_notifications
                                (user_id, discussion_id, new_atom_count, notification_type)
                            VALUES (%s, %s, 0, 'published')
                            ON CONFLICT (user_id, discussion_id) WHERE read = false
                            DO UPDATE SET notification_type = 'published';
                            """,
                            (str(user_uuid), str(disc_id)),
                        )
                    except Exception:
                        pass

            conn.commit()

        # Notify users whose topic affinity matches this discussion — targeted routing
        _notify_matched_users(str(disc_id), tags, str(user_uuid) if user_uuid else None, db_url)

        _logger.info("feed_publisher: published discussion %s from low-confidence turn", disc_id)
        return str(disc_id)
    except Exception as exc:
        _logger.warning("feed_publisher: failed to publish discussion: %s", exc)
        return None


def _notify_matched_users(
    disc_id: str,
    tags: list[str],
    exclude_user_id: str | None,
    db_url: str,
) -> None:
    """Insert user_notifications for users whose atom corpus overlaps with these tags.

    Non-fatal. Targeted routing is additive — if no matches are found, the
    discussion is still visible to everyone via the broadcast explore feed.
    """
    try:
        from app.topic_affinity import find_users_with_affinity
        matched_ids = find_users_with_affinity(tags, exclude_user_id, db_url)
        if not matched_ids:
            return

        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                for user_id in matched_ids:
                    try:
                        cur.execute(
                            """
                            INSERT INTO user_notifications
                                (user_id, discussion_id, new_atom_count)
                            VALUES (%s::uuid, %s::uuid, 0)
                            ON CONFLICT DO NOTHING;
                            """,
                            (user_id, disc_id),
                        )
                    except Exception:
                        pass
            conn.commit()
        _logger.info(
            "feed_publisher: notified %d matched users for discussion %s",
            len(matched_ids), disc_id,
        )
    except Exception as exc:
        _logger.debug("feed_publisher: targeted notify failed (non-fatal): %s", exc)
