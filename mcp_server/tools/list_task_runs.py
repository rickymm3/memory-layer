from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config


def list_task_runs(
    scope: str | None = None,
    outcome: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent task_run records, newest first.

    Queries the task_runs table only. No embeddings generated; results are
    ordered by created_at DESC. All inputs validated and passed as
    parameterized query parameters; no user input is interpolated into SQL.

    Args:
        scope: Optional scope filter (e.g. 'project:memory-layer').
               If omitted, returns runs across all scopes.
        outcome: Optional outcome filter. Must be 'success', 'partial', or
                 'failed'. Unrecognised values are ignored.
        limit: Maximum results to return. Clamped to 1–50. Default 10.
    """
    clamped_limit = max(1, min(int(limit), 50))
    VALID_OUTCOMES = {"success", "partial", "failed"}

    conditions: list[str] = []
    params: list[Any] = []

    if scope is not None:
        conditions.append("scope = %s")
        params.append(scope)

    if outcome is not None and outcome in VALID_OUTCOMES:
        conditions.append("outcome = %s")
        params.append(outcome)

    where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(clamped_limit)

    config = get_config()

    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    scope,
                    task_description,
                    model_used,
                    files_changed,
                    outcome,
                    lessons_stored,
                    created_at
                FROM task_runs
                {where_sql}
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                params,
            )
            rows = cur.fetchall()

    return [
        {
            "id": str(r[0]),
            "scope": r[1],
            "task_description": r[2],
            "model_used": r[3],
            "files_changed": r[4],
            "outcome": r[5],
            "lessons_stored": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]
