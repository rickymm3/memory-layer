#!/usr/bin/env python3
"""User management CLI for memory-layer.

This manages the identity anchor (who wrote what), not authentication.
Passwords, sessions, and tokens belong in the dashboard web layer.

Usage:
    python scripts/manage_users.py list
    python scripts/manage_users.py create --username alice --email alice@example.com
    python scripts/manage_users.py create --username bob
    python scripts/manage_users.py show --username alice
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


def list_users() -> None:
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.email, u.display_name, u.created_at,
                       COUNT(ms.id) AS signal_count
                FROM users u
                LEFT JOIN memory_signals ms ON ms.source_user_id = u.username
                GROUP BY u.id, u.username, u.email, u.display_name, u.created_at
                ORDER BY u.created_at;
            """)
            rows = cur.fetchall()
    if not rows:
        print("No users registered.")
        return
    print(f"{'Username':<20} {'Email':<30} {'Signals':>8}  Created")
    print("-" * 72)
    for r in rows:
        email = r[2] or "—"
        print(f"{r[1]:<20} {email:<30} {r[5]:>8}  {r[4].strftime('%Y-%m-%d')}")


def create_user(username: str, email: str | None, display_name: str | None) -> None:
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users (username, email, display_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (username) DO NOTHING
                RETURNING id, username;
            """, (username, email, display_name or username))
            row = cur.fetchone()
        conn.commit()
    if row:
        print(f"Created user: {row[1]} (id: {row[0]})")
    else:
        print(f"User '{username}' already exists.")


def show_user(username: str) -> None:
    cfg = get_config()
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT u.id, u.username, u.email, u.display_name, u.created_at,
                       COUNT(ms.id) AS signal_count,
                       COUNT(DISTINCT ms.memory_atom_id) AS atoms_touched
                FROM users u
                LEFT JOIN memory_signals ms ON ms.source_user_id = u.username
                WHERE u.username = %s
                GROUP BY u.id;
            """, (username,))
            row = cur.fetchone()
    if not row:
        print(f"User '{username}' not found.")
        return
    print(f"id:           {row[0]}")
    print(f"username:     {row[1]}")
    print(f"email:        {row[2] or '—'}")
    print(f"display_name: {row[3] or '—'}")
    print(f"created_at:   {row[4]}")
    print(f"signals:      {row[5]}")
    print(f"atoms_touched:{row[6]}")


def main() -> None:
    p = argparse.ArgumentParser(description="Memory-layer user management")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="List all registered users")

    c = sub.add_parser("create", help="Register a new user")
    c.add_argument("--username", required=True)
    c.add_argument("--email", default=None)
    c.add_argument("--display-name", default=None)

    s = sub.add_parser("show", help="Show user details")
    s.add_argument("--username", required=True)

    args = p.parse_args()
    if args.cmd == "list":
        list_users()
    elif args.cmd == "create":
        create_user(args.username, args.email, getattr(args, "display_name", None))
    elif args.cmd == "show":
        show_user(args.username)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
