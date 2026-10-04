#!/usr/bin/env python3
"""Manage who can read visibility='team' atoms in a scope.

A team atom in project:billing is readable by its owner and by every user
added to project:billing here. Private atoms are never shared this way.

Usage:
    python scripts/scope_members.py list --scope project:billing
    python scripts/scope_members.py add --scope project:billing --user alice
    python scripts/scope_members.py remove --scope project:billing --user alice
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
from app.memory_store import MemoryStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["list", "add", "remove"])
    parser.add_argument("--scope", required=True)
    parser.add_argument("--user")
    parser.add_argument("--role", default="member", choices=["member", "owner"])
    args = parser.parse_args()

    if args.action == "list":
        with psycopg.connect(get_config().database_url) as conn:
            rows = conn.execute(
                "SELECT user_id, role, created_at FROM scope_members WHERE scope = %s ORDER BY created_at;",
                (args.scope,),
            ).fetchall()
        for user_id, role, created_at in rows:
            print(f"{user_id:24} {role:8} {created_at:%Y-%m-%d}")
        if not rows:
            print(f"No members in {args.scope}.")
        return 0

    if not args.user:
        parser.error("--user is required for add/remove")
    store = MemoryStore.__new__(MemoryStore)  # no embedder needed
    store.config = get_config()
    if args.action == "add":
        store.add_scope_member(args.scope, args.user, args.role)
        print(f"Added {args.user} to {args.scope} as {args.role}.")
    else:
        removed = store.remove_scope_member(args.scope, args.user)
        print(f"Removed {args.user} from {args.scope}." if removed else f"{args.user} was not a member of {args.scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
