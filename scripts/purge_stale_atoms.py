#!/usr/bin/env python3
"""Purge atoms that haven't been accessed in over a year.

Safe to run anytime. Deletes atoms with last_accessed_at older than the
threshold AND excludes atoms that were never accessed (NULL last_accessed_at)
since those may be recent writes that just haven't been retrieved yet.

Usage:
    python scripts/purge_stale_atoms.py              # dry-run (default)
    python scripts/purge_stale_atoms.py --commit     # actually delete
    python scripts/purge_stale_atoms.py --days 180   # shorter cutoff
    python scripts/purge_stale_atoms.py --scope project:memory-layer
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


def purge_stale(days: int = 365, scope: str | None = None, commit: bool = False) -> dict:
    cfg = get_config()
    scope_clause = "AND scope = %(scope)s" if scope else ""
    params: dict = {"days": days, "scope": scope}

    preview_sql = f"""
        SELECT COUNT(*), MIN(last_accessed_at), MAX(last_accessed_at)
        FROM memory_atoms
        WHERE last_accessed_at IS NOT NULL
          AND last_accessed_at < NOW() - (%(days)s * INTERVAL '1 day')
          {scope_clause};
    """
    delete_sql = f"""
        DELETE FROM memory_atoms
        WHERE last_accessed_at IS NOT NULL
          AND last_accessed_at < NOW() - (%(days)s * INTERVAL '1 day')
          {scope_clause}
        RETURNING id;
    """

    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(preview_sql, params)
            count_row = cur.fetchone()
            candidate_count = int(count_row[0]) if count_row else 0
            oldest = count_row[1].isoformat() if count_row and count_row[1] else None
            newest = count_row[2].isoformat() if count_row and count_row[2] else None

        if commit and candidate_count > 0:
            with conn.cursor() as cur:
                cur.execute(delete_sql, params)
                deleted_ids = [str(r[0]) for r in cur.fetchall()]
            conn.commit()
            return {
                "deleted": len(deleted_ids),
                "oldest_accessed": oldest,
                "newest_accessed": newest,
                "scope": scope,
                "threshold_days": days,
                "committed": True,
            }

    return {
        "would_delete": candidate_count,
        "oldest_accessed": oldest,
        "newest_accessed": newest,
        "scope": scope,
        "threshold_days": days,
        "committed": False,
    }


def main() -> None:
    p = argparse.ArgumentParser(description="Purge stale memory atoms")
    p.add_argument("--days", type=int, default=365, help="Age cutoff in days (default: 365)")
    p.add_argument("--scope", type=str, default=None, help="Restrict to a specific scope")
    p.add_argument("--commit", action="store_true", help="Actually delete (default is dry-run)")
    args = p.parse_args()

    result = purge_stale(days=args.days, scope=args.scope, commit=args.commit)

    if args.commit:
        print(f"Deleted {result['deleted']} atoms older than {args.days} days.")
    else:
        print(f"Dry run: {result['would_delete']} atoms would be deleted (last accessed before {args.days} days ago).")
        print("Re-run with --commit to delete.")

    if result.get("oldest_accessed"):
        print(f"  Oldest access: {result['oldest_accessed']}")
        print(f"  Newest (of candidates): {result['newest_accessed']}")


if __name__ == "__main__":
    main()
