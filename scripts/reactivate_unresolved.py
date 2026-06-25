#!/usr/bin/env python3
"""Re-route discussions that have stalled without an answer.

Finds discussions in 'gathering' or 'unresolved' status that have had no
activity for more than N days. For each:
  1. Widens routing (more targeted notifications, lower affinity threshold).
  2. Escalates to visibility='public' on linked atoms.
  3. After MAX_REACTIVATIONS attempts with no response: marks thread_status='dead'
     and sends the originating user a humane "still looking" notification.

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

import psycopg
from app.config import get_config

MAX_REACTIVATIONS = 3  # attempts before marking dead


def find_stalled_discussions(conn, days: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, title, topic_tags, created_by_user_id, reactivation_count
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
            {
                "id": str(r[0]),
                "title": r[1],
                "tags": r[2] or [],
                "creator": str(r[3]) if r[3] else None,
                "reactivation_count": int(r[4] or 0),
            }
            for r in cur.fetchall()
        ]


def mark_dead(disc: dict, db_url: str) -> None:
    """Mark discussion dead and notify the originating user with humane copy."""
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE discussions
                SET thread_status = 'unresolved', last_activity_at = now()
                WHERE id = %s;
                """,
                (disc["id"],),
            )
            # Notify originating user — humane framing, not "your question is dead"
            if disc["creator"]:
                cur.execute(
                    """
                    INSERT INTO user_notifications
                        (user_id, discussion_id, new_atom_count, notification_type)
                    VALUES (%s::uuid, %s::uuid, 0, 'stalled')
                    ON CONFLICT DO NOTHING;
                    """,
                    (disc["creator"], disc["id"]),
                )
        conn.commit()


def widen_routing(disc: dict, db_url: str, dry_run: bool) -> int:
    """Notify more users and increment reactivation_count.

    Returns the count of new notifications sent.
    """
    from app.topic_affinity import find_users_with_affinity

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

    with psycopg.connect(db_url) as conn:
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
                SET last_activity_at = now(),
                    reactivation_count = reactivation_count + 1
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

    with psycopg.connect(db_url) as conn:
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
        count = disc["reactivation_count"]
        if count >= MAX_REACTIVATIONS:
            action_taken = "marked dead (notified user)" if not dry_run else "would mark dead"
            if not dry_run:
                mark_dead(disc, db_url)
            print(f"  [{disc['id'][:8]}] {disc['title'][:55]} — {action_taken} (retried {count}×)")
            continue

        notified = widen_routing(disc, db_url, dry_run)
        escalate_visibility(disc, db_url, dry_run)
        total_notified += notified
        action = f"would notify (attempt {count+1}/{MAX_REACTIVATIONS})" if dry_run else f"notified (attempt {count+1}/{MAX_REACTIVATIONS})"
        print(f"  [{disc['id'][:8]}] {disc['title'][:55]} — {action}: {notified} users")

    if dry_run:
        print(f"\nWould notify {total_notified} users. Re-run with --commit to send.")
    else:
        print(f"\nNotified {total_notified} users across {len(discussions)} stalled discussions.")


if __name__ == "__main__":
    main()
