"""Shared utilities for the memory-layer application."""
from __future__ import annotations

import re
from typing import Any


def slugify(title: str) -> str:
    """Convert a post title to a URL-safe slug."""
    s = re.sub(r'[^a-z0-9 ]', '', title.lower().strip())
    s = re.sub(r' +', '-', s)
    return s[:60].rstrip('-') or 'post'


def unique_slug(conn: Any, title: str, exclude_id: str | None = None) -> str:
    """Return a slug derived from title that doesn't already exist in social_posts.

    If the base slug is taken, appends -2, -3, ... until a free one is found.
    exclude_id: UUID string of the post being updated (skipped in uniqueness check).
    """
    base = slugify(title)
    slug = base
    i = 2
    while True:
        with conn.cursor() as cur:
            if exclude_id:
                cur.execute(
                    "SELECT COUNT(*) FROM social_posts WHERE slug = %s AND id != %s::uuid;",
                    (slug, exclude_id),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM social_posts WHERE slug = %s;",
                    (slug,),
                )
            if cur.fetchone()[0] == 0:
                return slug
        slug = f"{base}-{i}"
        i += 1
