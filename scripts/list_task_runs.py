#!/usr/bin/env python3
"""List task_runs stored in the memory layer.

Usage:
    python scripts/list_task_runs.py                       # all runs, newest first
    python scripts/list_task_runs.py --scope project:foo   # filter by scope
    python scripts/list_task_runs.py --outcome failed      # filter by outcome
    python scripts/list_task_runs.py --id <uuid>           # show one run + its lessons
    python scripts/list_task_runs.py --limit 5
"""
from __future__ import annotations

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

_SEP = "─" * 62


def main() -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="List task_run records.")
    parser.add_argument("--scope", default=None, help="Filter by scope")
    parser.add_argument(
        "--outcome",
        choices=["success", "partial", "failed"],
        default=None,
        help="Filter by outcome",
    )
    parser.add_argument(
        "--id",
        dest="task_run_id",
        default=None,
        help="Show one task_run by UUID (also prints linked lessons)",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max rows (default 20)")
    args = parser.parse_args()

    database_url = os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL).strip()
    try:
        conn = psycopg.connect(database_url)
    except Exception as exc:
        print(f"[ERROR] Could not connect to database: {exc}", file=sys.stderr)
        return 1

    # ── Single run detail ─────────────────────────────────────────────────────
    if args.task_run_id:
        row = conn.execute(
            """
            SELECT id, scope, task_description, model_used,
                   files_changed, test_results, outcome, notes,
                   lessons_stored, created_at
            FROM task_runs WHERE id = %s
            """,
            (args.task_run_id,),
        ).fetchone()
        if not row:
            print(f"[ERROR] task_run not found: {args.task_run_id}", file=sys.stderr)
            return 1

        id_, scope, task, model, files, tests, outcome, notes, lessons, created_at = row
        print(_SEP)
        print(f"  id        : {id_}")
        print(f"  scope     : {scope}")
        print(f"  task      : {task}")
        print(f"  model     : {model or '(not recorded)'}")
        print(f"  outcome   : {outcome.upper()}")
        print(f"  lessons   : {lessons}")
        print(f"  created   : {created_at.isoformat()}")
        if files:
            print(f"  files     : {files}")
        if tests:
            print(f"  tests     : {tests}")
        if notes:
            print(f"  notes     : {notes}")

        # Linked lessons (memory_atoms via memory_signals FK)
        lessons_rows = conn.execute(
            """
            SELECT DISTINCT ma.id, ma.content, ma.memory_type, ma.lifecycle_status
            FROM memory_signals ms
            JOIN memory_atoms ma ON ma.id = ms.memory_atom_id
            WHERE ms.task_run_id = %s
            ORDER BY ma.id
            """,
            (str(id_),),
        ).fetchall()
        print(_SEP)
        if lessons_rows:
            print(f"  Linked lessons ({len(lessons_rows)}):")
            for atom_id, content, mtype, status in lessons_rows:
                flag = f"  [{status}]" if status != "active" else ""
                print(f"\n    [{mtype}]{flag}")
                print(f"    {str(atom_id)[:8]}  {content[:80]}")
        else:
            print("  No linked lessons found.")
        print(_SEP)
        return 0

    # ── List mode ─────────────────────────────────────────────────────────────
    if args.limit <= 0:
        print("[ERROR] --limit must be > 0", file=sys.stderr)
        return 1

    conditions: list[str] = []
    params: list = []
    if args.scope:
        conditions.append("scope = %s")
        params.append(args.scope)
    if args.outcome:
        conditions.append("outcome = %s")
        params.append(args.outcome)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(args.limit)

    rows = conn.execute(
        f"""
        SELECT id, scope, task_description, model_used, outcome, lessons_stored, created_at
        FROM task_runs
        {where}
        ORDER BY created_at DESC
        LIMIT %s
        """,
        params,
    ).fetchall()

    if not rows:
        print("No task_runs found.")
        return 0

    print(f"\n{'ID':8}  {'SCOPE':28}  {'OUT':8}  {'LSN':4}  {'MODEL':16}  TASK")
    print(_SEP)
    for id_, scope, task, model, outcome, lessons, created_at in rows:
        short_id = str(id_)[:8]
        short_scope = (scope or "")[:28]
        short_model = (model or "-")[:16]
        short_task = task[:40] + ("…" if len(task) > 40 else "")
        print(
            f"{short_id}  {short_scope:28}  {outcome:8}  {lessons:4}  "
            f"{short_model:16}  {short_task}"
        )
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
