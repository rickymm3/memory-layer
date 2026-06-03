#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DATABASE_URL = (
    "postgresql://memory:memory_dev_password@localhost:5432/memory_layer_development"
)


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="List recent memory signals.")
    parser.add_argument("--scope", default=None, help="Filter by scope")
    parser.add_argument(
        "--type", dest="memory_type", default=None, help="Filter by memory_type"
    )
    parser.add_argument(
        "--relationship", default=None, help="Filter by relationship"
    )
    parser.add_argument(
        "--source-key", dest="source_key", default=None, help="Filter by source_key"
    )
    parser.add_argument("--limit", type=int, default=20, help="Max rows to return")
    args = parser.parse_args()

    if args.limit <= 0:
        print("[ERROR] --limit must be > 0", file=sys.stderr)
        return 1

    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()

    try:
        conn = psycopg.connect(database_url)
    except Exception as exc:
        print(f"[ERROR] Could not connect to database: {exc}", file=sys.stderr)
        return 1

    conditions: list[str] = []
    params: list = []

    if args.scope:
        conditions.append("scope = %s")
        params.append(args.scope)
    if args.memory_type:
        conditions.append("memory_type = %s")
        params.append(args.memory_type)
    if args.relationship:
        conditions.append("relationship = %s")
        params.append(args.relationship)
    if args.source_key:
        conditions.append("source_key = %s")
        params.append(args.source_key)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(args.limit)

    query = f"""
        SELECT
            id,
            memory_atom_id,
            source_key,
            source_type,
            memory_type,
            scope,
            relationship,
            content,
            created_at
        FROM memory_signals
        {where}
        ORDER BY created_at DESC
        LIMIT %s;
    """

    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception as exc:
        print(f"[ERROR] Query failed: {exc}", file=sys.stderr)
        conn.close()
        return 1
    finally:
        conn.close()

    if not rows:
        print("No signals found.")
        return 0

    for row in rows:
        id_, atom_id, source_key, source_type, mem_type, scope, rel, content, created_at = row
        print(f"id:                   {id_}")
        print(f"memory_atom_id:       {atom_id if atom_id else '(none)'}")
        print(f"source_key:           {source_key}")
        print(f"source_type:          {source_type}")
        print(f"memory_type:          {mem_type}")
        print(f"scope:                {scope if scope else '(global)'}")
        print(f"relationship:         {rel if rel else '(none)'}")
        print(f"content:              {content}")
        print(f"created_at:           {created_at}")
        print("-" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
