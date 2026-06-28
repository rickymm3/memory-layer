"""User profile signal updates: interest vector and topic credibility.

Interest vector: a decaying centroid of post embeddings the user has
engaged with. Updated immediately on view, reaction, and perspective
contribution. Computed in Python (no reliance on specific pgvector
operator versions for scalar multiplication).

Credibility: a per-(user, topic_tag) running average of reaction
deltas from posts the user contributed to. Batch-updated by
post_worker via apply_pending_credibility_reactions().
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from app.config import get_config

_log = logging.getLogger(__name__)

# Weight of existing interest_vector when blending a new post signal.
# Higher = slower to change, more stable long-term profile.
_ALPHA = 0.85

# Per-signal weights: how strongly each interaction type moves the vector.
_WEIGHT_VIEW         = 1.0
_WEIGHT_UPVOTE       = 2.5
_WEIGHT_DOWNVOTE     = 0.3  # downvotes still inform interests, just weakly
_WEIGHT_PERSPECTIVE  = 4.0  # contributing your own text is the strongest signal


def record_post_view(username: str, post_id: str) -> None:
    """Mark post as viewed and blend its embedding into the user's interest vector."""
    _update(username, post_id, _WEIGHT_VIEW, mark_viewed=True)


def record_post_reaction(username: str, post_id: str, vote: str) -> None:
    """Blend post embedding into interest vector after a reaction."""
    weight = _WEIGHT_UPVOTE if vote == "up" else _WEIGHT_DOWNVOTE
    _update(username, post_id, weight, mark_viewed=False)


def record_perspective_contribution(username: str, post_id: str) -> None:
    """Strongest signal: user authored a perspective on this post."""
    _update(username, post_id, _WEIGHT_PERSPECTIVE, mark_viewed=True)


# ── Interest vector blend ─────────────────────────────────────────────────────

def _update(
    username: str,
    post_id: str,
    weight: float,
    mark_viewed: bool,
) -> None:
    cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                # Fetch user id, current interest_vector, and post embedding
                cur.execute(
                    """
                    SELECT u.id, u.interest_vector, sp.embedding
                    FROM users u, social_posts sp
                    WHERE u.username = %s AND sp.id = %s::uuid;
                    """,
                    (username, post_id),
                )
                row = cur.fetchone()
                if not row:
                    return
                user_id, old_vec, post_emb = row

                if mark_viewed and user_id:
                    cur.execute(
                        """
                        INSERT INTO post_reach (post_id, user_id, viewed)
                        VALUES (%s::uuid, %s, true)
                        ON CONFLICT (post_id, user_id)
                        DO UPDATE SET viewed = true;
                        """,
                        (post_id, user_id),
                    )

                if post_emb is None:
                    conn.commit()
                    return

                new_vec = _blend(old_vec, post_emb, weight)
                cur.execute(
                    "UPDATE users SET interest_vector = %s WHERE username = %s;",
                    (new_vec, username),
                )
            conn.commit()
    except Exception as exc:
        _log.debug("profile_updater._update: %s", exc)


def _blend(old_vec: Any, new_vec: Any, weight: float) -> list[float]:
    """Return a weighted blend of old and new vectors, L2-normalized.

    new_vec is scaled by `weight` before blending so stronger signals
    move the centroid further. Result is always unit-length.
    """
    if old_vec is None:
        # No history yet — new_vec becomes the baseline
        raw = list(new_vec)
    else:
        old = list(old_vec)
        nw = list(new_vec)
        effective_new_weight = (1.0 - _ALPHA) * weight
        raw = [
            _ALPHA * o + effective_new_weight * n
            for o, n in zip(old, nw)
        ]

    # L2 normalize
    magnitude = sum(x * x for x in raw) ** 0.5
    if magnitude > 0:
        return [x / magnitude for x in raw]
    return raw


# ── Topic credibility batch update ────────────────────────────────────────────

def apply_pending_credibility_reactions(cfg: Any | None = None) -> None:
    """Process unapplied post_reactions and update user_topic_credibility.

    Called from the post_worker loop. Marks each reaction credibility_applied=true
    after processing so it's never double-counted.
    """
    if cfg is None:
        cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pr.post_id::text, pr.user_id::text, pr.vote,
                           sp.topic_tags, sp.author_user_id::text
                    FROM post_reactions pr
                    JOIN social_posts sp ON sp.id = pr.post_id
                    WHERE pr.credibility_applied = false
                      AND sp.topic_tags IS NOT NULL
                      AND array_length(sp.topic_tags, 1) > 0
                    LIMIT 200;
                    """
                )
                pending = cur.fetchall()

            if not pending:
                return

            with conn.cursor() as cur:
                for post_id, reactor_id, vote, topic_tags, author_id in pending:
                    # Attribute signal to all contributors (author + perspective authors)
                    cur.execute(
                        """
                        SELECT DISTINCT contributor_id FROM (
                            SELECT pc.user_id::text AS contributor_id
                            FROM post_contributors pc WHERE pc.post_id = %s::uuid
                            UNION
                            SELECT p.author_user_id::text
                            FROM perspectives p
                            WHERE p.post_id = %s::uuid AND p.author_user_id IS NOT NULL
                            UNION
                            SELECT %s
                        ) sub
                        WHERE contributor_id IS NOT NULL
                          AND contributor_id != %s;
                        """,
                        (post_id, post_id, author_id, reactor_id),
                    )
                    contributors = [r[0] for r in cur.fetchall()]

                    delta = 0.3 if vote == "up" else -0.1

                    for contrib_id in contributors:
                        for tag in (topic_tags or [])[:5]:
                            cur.execute(
                                """
                                INSERT INTO user_topic_credibility
                                    (user_id, topic_tag, score, sample_count)
                                VALUES (%s::uuid, %s, %s, 1)
                                ON CONFLICT (user_id, topic_tag) DO UPDATE
                                SET score = (
                                        user_topic_credibility.score
                                        * user_topic_credibility.sample_count
                                        + EXCLUDED.score
                                    ) / (user_topic_credibility.sample_count + 1),
                                    sample_count = user_topic_credibility.sample_count + 1,
                                    updated_at = now();
                                """,
                                (contrib_id, tag, delta),
                            )

                    cur.execute(
                        """
                        UPDATE post_reactions SET credibility_applied = true
                        WHERE post_id = %s::uuid AND user_id = %s::uuid;
                        """,
                        (post_id, reactor_id),
                    )

            conn.commit()
            _log.info("profile_updater: applied credibility for %d reactions", len(pending))
    except Exception as exc:
        _log.debug("profile_updater.apply_pending_credibility_reactions: %s", exc)
