"""Personalized feed ranking for the Synapse home page.

Returns raw tuples — formatting (timestamps, categories) is the caller's job.

Sort modes:
  for_you   — personalized; excludes already-viewed/responded posts (not own)
  new       — published_at DESC; full feed, no exclusions
  popular   — reach_score DESC; full feed, no exclusions
  rising    — velocity score DESC; full feed, no exclusions

'For You' personalised ranking weights:
  50% semantic similarity to user's interest_vector
  30% freshness — half-life ~1 week based on last_activity_at
  20% activity — log(perspective_count + 1) * log(reach_score + 1)

Cold start (no interest_vector): rising sort, still excludes already-seen.
Own posts are NEVER excluded — users need to see their own published content.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from app.config import get_config

_log = logging.getLogger(__name__)

FEED_LIMIT = 40

# Column order returned by every query variant:
# (id, title, format, topic_tags, published_at, author_username,
#  excerpt, perspective_count, reach_score, slug)
_SELECT = """
    SELECT sp.id, sp.title, sp.format, sp.topic_tags,
           sp.published_at, u.username,
           LEFT(sp.body, 280) AS excerpt,
           sp.perspective_count, sp.reach_score, sp.slug
    FROM social_posts sp
    LEFT JOIN users u ON u.id = sp.author_user_id
    WHERE sp.status = 'published'
"""

# Only applied on 'for_you' — show things you haven't engaged with yet.
# Own posts are not excluded: you should see your own published work.
_EXCLUDE_SEEN = """
    AND NOT EXISTS (
        SELECT 1 FROM post_reach pr
        JOIN users u2 ON u2.id = pr.user_id
        WHERE pr.post_id = sp.id
          AND u2.username = %s
          AND (pr.viewed OR pr.responded)
    )
"""

_RISING_ORDER = """
    (sp.reach_score + sp.perspective_count * 2.0)
    / GREATEST(1, EXTRACT(EPOCH FROM (now() - sp.published_at)) / 604800.0) DESC
"""


def get_personalized_feed(
    username: str | None,
    category_keywords: list[str] | None = None,
    sort: str = "for_you",
    limit: int = FEED_LIMIT,
) -> list[tuple]:
    """Return ranked post tuples for the home feed.

    Returns list of (id, title, format, topic_tags, published_at,
    author_username, excerpt, perspective_count, reach_score).
    """
    cfg = get_config()
    try:
        with psycopg.connect(cfg.database_url) as conn:
            with conn.cursor() as cur:
                return _query(cur, username, category_keywords, sort, limit)
    except Exception as exc:
        _log.warning("feed_ranker: %s", exc)
        return []


def _query(
    cur: Any,
    username: str | None,
    category_keywords: list[str] | None,
    sort: str,
    limit: int,
) -> list[tuple]:
    cat_filter = ""
    base_params: list = []

    if category_keywords:
        cat_filter = "AND sp.topic_tags && %s::text[]"
        base_params.append(category_keywords)

    # ── Static sort modes — no personalization, no exclusions ─────────────────
    if sort in ("new", "popular", "rising") or not username:
        order = {
            "new":     "sp.published_at DESC",
            "popular": "sp.reach_score DESC",
            "rising":  _RISING_ORDER,
        }.get(sort, "sp.published_at DESC")

        params = list(base_params) + [limit]
        cur.execute(
            f"{_SELECT} {cat_filter} ORDER BY {order} LIMIT %s;",
            params,
        )
        return cur.fetchall()

    # ── for_you — personalized, excludes already-seen ─────────────────────────
    cur.execute(
        "SELECT interest_vector FROM users WHERE username = %s;",
        (username,),
    )
    row = cur.fetchone()
    interest_vector = row[0] if row and row[0] is not None else None

    if interest_vector is None:
        # Cold start: rising order, exclude already-seen posts
        params = list(base_params) + [username, limit]
        cur.execute(
            f"{_SELECT} {cat_filter} {_EXCLUDE_SEEN} ORDER BY {_RISING_ORDER} LIMIT %s;",
            params,
        )
        return cur.fetchall()

    # Full personalized ranking — exclude seen, rank by interest + freshness + activity
    params = list(base_params) + [username, interest_vector, limit]
    cur.execute(
        f"""
        {_SELECT}
          AND sp.embedding IS NOT NULL
          {cat_filter}
          {_EXCLUDE_SEEN}
        ORDER BY (
            (1.0 - (sp.embedding <=> %s::vector)) * 0.5
            + (1.0 / (1.0 + EXTRACT(EPOCH FROM (now() - sp.last_activity_at)) / 604800.0)) * 0.3
            + (LN(1.0 + sp.perspective_count) * LN(1.0 + GREATEST(sp.reach_score, 0.0))) * 0.2
        ) DESC
        LIMIT %s;
        """,
        params,
    )
    return cur.fetchall()
