"""Synapse's reactions to memory-core commit events.

The memory core (app/, mcp_server/) knows nothing about posts, discussions
or the feed. It emits events (app/events.py); these handlers turn them into
Synapse behaviour. They are registered when the synapse package is imported.
"""
from __future__ import annotations

import logging
import os
import threading

from app.events import on

_log = logging.getLogger(__name__)


def _db_url() -> str:
    return os.environ.get("DATABASE_URL", "")


def record_novelty(atom_id, novelty_score=0.0, interest_flag=False, **_):
    """Store the critic's novelty verdict, used by Synapse's feed ranking."""
    if not (novelty_score > 0 or interest_flag) or not _db_url():
        return
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            "UPDATE memory_atoms SET novelty_score=%s, interest_flag=%s WHERE id=%s;",
            (novelty_score, interest_flag, atom_id),
        )
        conn.commit()


def advance_discussions(atom_id, decision, **_):
    """Move linked discussion threads to 'updated' after a new atom lands.

    Only escalates; never overwrites answered/validated/reopened.
    """
    if decision != "commit" or not _db_url():
        return
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        conn.execute(
            """
            UPDATE discussions d
            SET thread_status = 'updated'
            FROM discussion_atoms da
            WHERE da.discussion_id = d.id
              AND da.atom_id = %s
              AND d.thread_status IN ('active', 'gathering');
            """,
            (atom_id,),
        )
        conn.commit()


def enqueue_post(atom_id, source_user_id, scope, visibility, **_):
    """Public atoms go to the post-generation queue. Private atoms never do."""
    if visibility != "public":
        return
    from synapse.post_worker import enqueue
    enqueue(atom_id, source_user_id or "local_user", scope)


def alert_related_discussions(atom_id, source_user_id, **_):
    """Notify the author when others have discussed the same topic (background)."""
    if not source_user_id:
        return
    threading.Thread(
        target=_related_discussion_alert, args=(atom_id, source_user_id), daemon=True
    ).start()


def _related_discussion_alert(atom_id: str, source_user_id: str) -> None:
    try:
        import psycopg
        db_url = _db_url()
        if not db_url:
            return
        with psycopg.connect(db_url) as conn:
            row = conn.execute(
                "SELECT topic_tags FROM memory_atoms WHERE id = %s AND lifecycle_status = 'active';",
                (atom_id,),
            ).fetchone()
            topic_tags = row[0] if row else None
            if not topic_tags:
                return
            related_ids = [
                str(r[0])
                for r in conn.execute(
                    """
                    SELECT d.id
                    FROM discussions d
                    WHERE d.auto_published = true
                      AND d.thread_status NOT IN ('unresolved', 'dead')
                      AND d.topic_tags && %s
                      AND d.created_by_user_id != (
                          SELECT id FROM users WHERE username = %s
                      )
                    ORDER BY d.last_activity_at DESC
                    LIMIT 3;
                    """,
                    (topic_tags, source_user_id),
                ).fetchall()
            ]
            if not related_ids:
                return
            # Single batch INSERT — avoids N round-trips at scale
            conn.execute(
                """
                INSERT INTO user_notifications
                    (user_id, discussion_id, new_atom_count, notification_type)
                SELECT u.id, d.id, 0, 'related_discussion'
                FROM users u
                CROSS JOIN unnest(%s::uuid[]) AS d(id)
                WHERE u.username = %s
                ON CONFLICT DO NOTHING;
                """,
                (related_ids, source_user_id),
            )
            conn.commit()
        _log.info("related_discussion_alert: notified %s of %d discussion(s)", source_user_id, len(related_ids))
    except Exception as exc:
        _log.debug("related_discussion_alert failed (non-fatal): %s", exc)


def requeue_posts(atom_id, **_):
    """A cited atom was superseded: regenerate the posts that cited it."""
    from synapse.post_worker import requeue_posts_for_atom
    requeue_posts_for_atom(atom_id)


def publish_direct_answers(user_msg, answer, committed_atom_ids, source_user_id, route, **_):
    """Chat turns answered without memory (route='direct') surface in the explore feed.

    Novel committed atoms are published with evidence; otherwise a genuinely
    unanswered question is routed so targeted users can respond.
    """
    if route != "direct":
        return
    from app.chat import _is_routeable_question
    from synapse.feed_publisher import _worth_surfacing, publish_to_feed

    db_url = _db_url()
    if committed_atom_ids and _worth_surfacing(committed_atom_ids, db_url):
        ids = committed_atom_ids
    elif _is_routeable_question(user_msg):
        ids = []
    else:
        return
    publish_to_feed(
        user_msg=user_msg,
        answer=answer,
        committed_atom_ids=ids,
        source_user_id=source_user_id,
        db_url=db_url,
    )


def register() -> None:
    on("atom_committed", record_novelty)
    on("atom_committed", advance_discussions)
    on("atom_committed", enqueue_post)
    on("atom_committed", alert_related_discussions)
    on("atom_superseded", requeue_posts)
    on("turn_reflected", publish_direct_answers)
