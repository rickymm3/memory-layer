#!/usr/bin/env python3
"""Re-route discussions that have stalled without an answer.

Finds discussions in 'gathering' or 'unresolved' status that have had no
activity for more than N days. For each:
  1. Widens routing by lowering the affinity threshold to reach more users.
  2. Escalates to visibility='public' on linked atoms so any browsing user
     can contribute.

Safe to run repeatedly (idempotent — ON CONFLICT DO NOTHING on notifications).

Usage:
    python scripts/reactivate_unresolved.py              # dry-run
    python scripts/reactivate_unresolved.py --commit     # widen routing
    python scripts/reactivate_unresolved.py --days 3     # shorter staleness window
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import os
import psycopg
from app.config import get_config


def find_stalled_discussions(conn, days: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, topic_tags, created_by_user_id
            FROM discussions
            WHERE thread_status IN ('gathering', 'unresolved')
              AND auto_published = true
              AND last_activity_at < NOW() - (%s * INTERVAL '1 day')
            ORDER BY last_activity_at ASC
            LIMIT 50;
            """,
            (days,),
        )
        return [
            {"id": str(r[0]), "title": r[1], "tags": r[2] or [], "creator": str(r[3]) if r[3] else None}
            for r in cur.fetchall()
        ]


def widen_routing(disc: dict, db_url: str, dry_run: bool) -> int:
    """Notify more users by using a lower affinity threshold.

    Returns the count of new notifications sent.
    """
    from app.topic_affinity import find_users_with_affinity

    # Lower threshold: pass tags directly, limit is wider (50 → 100)
    matched_ids = find_users_with_affinity(
        disc["tags"],
        exclude_user_id=disc["creator"],
        db_url=db_url,
        limit=100,
    )
    if not matched_ids:
        return 0

    if dry_run:
        return len(matched_ids)

    import psycopg as _pg
    with _pg.connect(db_url) as conn:
        with conn.cursor() as cur:
            sent = 0
            for user_id in matched_ids:
                try:
                    cur.execute(
                        """
                        INSERT INTO user_notifications
                            (user_id, discussion_id, new_atom_count, notification_type)
                        VALUES (%s::uuid, %s::uuid, 0, 'targeted')
                        ON CONFLICT DO NOTHING;
                        """,
                        (user_id, disc["id"]),
                    )
                    sent += cur.rowcount
                except Exception:
                    pass
            cur.execute(
                """
                UPDATE discussions
                SET last_activity_at = now()
                WHERE id = %s;
                """,
                (disc["id"],),
            )
        conn.commit()
    return sent


def escalate_visibility(disc: dict, db_url: str, dry_run: bool) -> int:
    """Make linked atoms public so browsing users can discover them."""
    if dry_run:
        return 0

    import psycopg as _pg
    with _pg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memory_atoms ma
                SET visibility = 'public'
                FROM discussion_atoms da
                WHERE da.discussion_id = %s
                  AND da.atom_id = ma.id
                  AND ma.visibility = 'private';
                """,
                (disc["id"],),
            )
            updated = cur.rowcount
        conn.commit()
    return updated


def main() -> None:
    p = argparse.ArgumentParser(description="Re-route stalled discussions")
    p.add_argument("--days", type=int, default=7, help="Staleness threshold in days (default: 7)")
    p.add_argument("--commit", action="store_true", help="Actually widen routing (default is dry-run)")
    args = p.parse_args()

    cfg = get_config()
    db_url = cfg.database_url
    dry_run = not args.commit

    with psycopg.connect(db_url) as conn:
        discussions = find_stalled_discussions(conn, args.days)

    print(f"{'DRY RUN — ' if dry_run else ''}Found {len(discussions)} stalled discussion(s) (>{args.days}d inactive)")

    total_notified = 0
    for disc in discussions:
        notified = widen_routing(disc, db_url, dry_run)
        escalate_visibility(disc, db_url, dry_run)
        total_notified += notified
        action = "would notify" if dry_run else "notified"
        print(f"  [{disc['id'][:8]}] {disc['title'][:60]} — {action} {notified} users")

    if dry_run:
        print(f"\nWould notify {total_notified} users. Re-run with --commit to send.")
    else:
        print(f"\nNotified {total_notified} users across {len(discussions)} stalled discussions.")


if __name__ == "__main__":
    main()
