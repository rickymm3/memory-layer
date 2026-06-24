#!/usr/bin/env python3
"""Apply all pending SQL migrations in db/migrations/ in filename order.

Tracks applied migrations in a schema_migrations table so each runs exactly once.
Safe to run multiple times (idempotent).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()


def apply_migrations(database_url: str) -> None:
    migrations_dir = Path(__file__).parent / "migrations"
    sql_files = sorted(migrations_dir.glob("*.sql"))

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    filename TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                """
            )
        conn.commit()

        for path in sql_files:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM schema_migrations WHERE filename = %s;",
                    (path.name,),
                )
                if cur.fetchone():
                    print(f"  skip  {path.name}")
                    continue

            sql = path.read_text()
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s);",
                    (path.name,),
                )
            conn.commit()
            print(f"  apply {path.name}")

    print("Migrations done.")


if __name__ == "__main__":
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL not set.", file=sys.stderr)
        sys.exit(1)
    apply_migrations(url)
