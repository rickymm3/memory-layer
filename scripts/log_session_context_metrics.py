#!/usr/bin/env python3
# Uses project venv — invoke via "$CLAUDE_PROJECT_DIR/.venv/bin/python3 $CLAUDE_PROJECT_DIR/scripts/log_session_context_metrics.py"
"""PostToolUse hook helper — logs session-start context metrics to context_size_log.

Reads PostToolUse JSON from stdin (fired after memory_task_context completes),
extracts the atom list from the tool response, and writes one row to
context_size_log with turn_number=0 to represent the session-start injection cost.

This enables /context-stats to show how many tokens are spent before turn 1.
"""
import json
import os
import sys


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        sys.exit(0)  # never crash on bad input

    session_id = data.get("session_id")
    tool_response = data.get("tool_response") or {}

    # Collect all atoms injected by memory_task_context
    atoms: list[dict] = []
    for key in ("project_context", "task_relevant_atoms", "model_lessons"):
        atoms.extend(tool_response.get(key) or [])

    # Also count recent_task_runs as context overhead
    task_runs = tool_response.get("recent_task_runs") or []

    retrieved_atom_count = len(atoms)
    # Estimate tokens from atom content: sum of content lengths / 4
    retrieved_atom_tokens = sum(len(a.get("content", "")) // 4 for a in atoms)
    retrieved_atom_tokens += sum(
        len(str(r.get("task_description", "") or "")) // 4 for r in task_runs
    )

    if retrieved_atom_count == 0 and not task_runs:
        sys.exit(0)  # nothing to log

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        # Try loading from .env in the project directory
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
        env_path = os.path.join(project_dir, ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("DATABASE_URL="):
                        database_url = line.split("=", 1)[1].strip()
                        break

    if not database_url:
        sys.exit(0)

    try:
        import psycopg
        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO context_size_log
                        (session_id, turn_number, retrieved_atom_count, used_atom_count,
                         retrieved_atom_tokens, user_tokens, assistant_tokens)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (session_id, 0, retrieved_atom_count, retrieved_atom_count,
                     retrieved_atom_tokens, 0, 0),
                )
            conn.commit()
    except Exception:
        pass  # never crash the hook pipeline


if __name__ == "__main__":
    main()
