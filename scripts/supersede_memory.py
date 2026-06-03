#!/usr/bin/env python3
"""Set a memory atom's lifecycle status to superseded, deprecated, or archived.

Usage
-----
# Supersede old belief, point to replacement:
    .venv/bin/python scripts/supersede_memory.py <old_atom_id> <new_atom_id> \
        --reason "Write policy revised to policy-based approach."

# Deprecate without a replacement:
    .venv/bin/python scripts/supersede_memory.py <old_atom_id> \
        --status deprecated \
        --reason "No longer relevant to current architecture."

# Archive (hide from retrieval, inspect only):
    .venv/bin/python scripts/supersede_memory.py <old_atom_id> \
        --status archived \
        --reason "Historical record only."

Rules
-----
* This script NEVER deletes memory_atoms or memory_signals rows.
* Signals remain immutable and intact.
* confidence / aggregation fields are NOT changed.
* The superseded atom is still retrievable via memory_get (inspection).
* The superseded atom is excluded from memory_project_context.
* 'archived' atoms are excluded from memory_search and memory_recent too.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config

_VALID_STATUSES = ("superseded", "deprecated", "archived", "contested")


def _fetch_atom(conn: psycopg.Connection, atom_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, content, lifecycle_status, superseded_by_atom_id,
                   lifecycle_reason, lifecycle_updated_at
            FROM memory_atoms WHERE id = %s;
            """,
            (atom_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": str(row[0]),
        "content": row[1],
        "lifecycle_status": row[2],
        "superseded_by_atom_id": str(row[3]) if row[3] else None,
        "lifecycle_reason": row[4],
        "lifecycle_updated_at": row[5].isoformat() if row[5] else None,
    }


def set_lifecycle_status(
    old_atom_id: str,
    status: str,
    *,
    new_atom_id: str | None = None,
    reason: str | None = None,
) -> dict:
    """Update lifecycle fields on a memory atom.

    Returns a report dict with before/after state.
    Does NOT delete any atoms or signals.
    """
    if status not in _VALID_STATUSES:
        return {"error": f"Invalid status {status!r}. Must be one of: {', '.join(_VALID_STATUSES)}"}

    config = get_config()
    now = datetime.now(tz=timezone.utc)

    with psycopg.connect(config.database_url) as conn:
        before = _fetch_atom(conn, old_atom_id)
        if before is None:
            return {"error": f"Atom not found: {old_atom_id}"}

        if new_atom_id is not None:
            after_check = _fetch_atom(conn, new_atom_id)
            if after_check is None:
                return {"error": f"Replacement atom not found: {new_atom_id}"}

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE memory_atoms
                SET lifecycle_status      = %s,
                    superseded_by_atom_id = %s,
                    lifecycle_reason      = %s,
                    lifecycle_updated_at  = %s
                WHERE id = %s;
                """,
                (status, new_atom_id, reason, now, old_atom_id),
            )
        conn.commit()

        after = _fetch_atom(conn, old_atom_id)

    return {
        "updated": True,
        "old_atom_id": old_atom_id,
        "status": status,
        "superseded_by_atom_id": new_atom_id,
        "reason": reason,
        "lifecycle_updated_at": now.isoformat(),
        "before": before,
        "after": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Mark a memory atom as superseded, deprecated, or archived.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("old_atom_id", help="UUID of the atom to update.")
    parser.add_argument(
        "new_atom_id",
        nargs="?",
        default=None,
        help="UUID of the replacement atom (required when --status=superseded).",
    )
    parser.add_argument(
        "--status",
        default="superseded",
        choices=list(_VALID_STATUSES),
        help="New lifecycle status (default: superseded).",
    )
    parser.add_argument("--reason", default=None, help="Human-readable explanation.")
    args = parser.parse_args()

    if args.status == "superseded" and args.new_atom_id is None:
        print(
            "[WARN] --status=superseded but no new_atom_id provided. "
            "superseded_by_atom_id will be NULL.",
            file=sys.stderr,
        )

    result = set_lifecycle_status(
        old_atom_id=args.old_atom_id,
        status=args.status,
        new_atom_id=args.new_atom_id,
        reason=args.reason,
    )

    if "error" in result:
        print(f"[ERROR] {result['error']}", file=sys.stderr)
        return 1

    print(f"Updated atom: {result['old_atom_id']}")
    print(f"  status:               {result['before']['lifecycle_status']} → {result['status']}")
    if result["superseded_by_atom_id"]:
        print(f"  superseded_by:        {result['superseded_by_atom_id']}")
    if result["reason"]:
        print(f"  reason:               {result['reason']}")
    print(f"  lifecycle_updated_at: {result['lifecycle_updated_at'][:19]}")
    print(f"  content:              {result['before']['content'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
