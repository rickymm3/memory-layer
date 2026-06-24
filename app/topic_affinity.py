"""Topic affinity inference from a user's atom corpus.

Derives which topics a user is interested in by analysing the content
of atoms they've generated (via signals). Used to:
  1. Rank the /explore feed — discussions matching the user's topics rise
  2. Target notifications — alert users whose affinity matches a new discussion

No ML required. Word-frequency over recent atoms is enough at this scale.
Broadcast fallback is always honoured: a user with zero atoms gets the
unranked feed rather than an empty one.
"""
from __future__ import annotations

import re
from functools import lru_cache

_STOP = {
    "about", "after", "again", "also", "always", "another", "because",
    "before", "being", "between", "could", "every", "first", "from",
    "have", "their", "there", "these", "thing", "think", "those",
    "through", "using", "where", "which", "while", "would", "should",
    "other", "people", "really", "right", "since", "something", "still",
    "things", "though", "very", "however", "whether", "without",
}

_ATOM_LIMIT = 200  # most recent atoms to scan per user


def get_user_topic_tags(username: str, db_url: str) -> list[str]:
    """Return the top topic words derived from a user's atom corpus.

    Returns an empty list if the user has no atoms (broadcast fallback applies).
    Non-fatal — any DB error returns [].
    """
    if not username or not db_url:
        return []
    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ma.content
                    FROM memory_atoms ma
                    JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                    WHERE ms.source_user_id = %s
                    ORDER BY ms.created_at DESC
                    LIMIT %s;
                    """,
                    (username, _ATOM_LIMIT),
                )
                rows = cur.fetchall()
    except Exception:
        return []

    if not rows:
        return []

    combined = " ".join(r[0] for r in rows if r[0]).lower()
    words = re.findall(r"[a-z]{5,}", combined)
    freq: dict[str, int] = {}
    for w in words:
        if w not in _STOP:
            freq[w] = freq.get(w, 0) + 1

    ranked = sorted(freq, key=lambda w: -freq[w])
    return ranked[:20]


def rank_discussions_by_affinity(
    discussions: list[dict],
    user_tags: list[str],
) -> list[dict]:
    """Re-rank a list of discussion dicts by tag overlap with user_tags.

    Discussions with no tag overlap are kept at the bottom (broadcast fallback).
    If user_tags is empty the original order is returned unchanged.
    """
    if not user_tags:
        return discussions

    user_set = set(user_tags)

    def _score(d: dict) -> float:
        disc_tags = set(d.get("topic_tags") or [])
        title_words = set(re.findall(r"[a-z]{5,}", (d.get("title") or "").lower()))
        tag_overlap = len(disc_tags & user_set) + len(title_words & user_set)
        # Boost for discussions with high contributor counts — behavioral signal
        contributor_signal = min(d.get("contributor_count", 0) / 10.0, 1.0) * 0.5
        # Boost for answered/validated — they're worth returning to
        status_boost = 0.3 if d.get("thread_status") in ("answered", "validated") else 0.0
        return tag_overlap + contributor_signal + status_boost

    return sorted(discussions, key=_score, reverse=True)


def find_users_with_affinity(
    tags: list[str],
    exclude_user_id: str | None,
    db_url: str,
    limit: int = 30,
) -> list[str]:
    """Return user IDs whose atom corpus overlaps with the given topic tags.

    Used to target notifications when a new discussion is published.
    Returns UUIDs — caller is responsible for deduplication.
    """
    if not tags or not db_url:
        return []

    # Build ILIKE patterns — one per tag word
    patterns = [f"%{t}%" for t in tags[:5]]
    try:
        import psycopg
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                # Find users who have atoms containing any of the tag words
                cur.execute(
                    """
                    SELECT DISTINCT u.id
                    FROM users u
                    JOIN memory_signals ms ON ms.source_user_id = u.username
                    JOIN memory_atoms ma ON ma.id = ms.memory_atom_id
                    WHERE (
                        ma.content ILIKE ANY(%s)
                    )
                    AND (%s IS NULL OR u.id != %s::uuid)
                    AND u.is_active = true
                    LIMIT %s;
                    """,
                    (patterns, exclude_user_id, exclude_user_id, limit),
                )
                return [str(r[0]) for r in cur.fetchall()]
    except Exception:
        return []
