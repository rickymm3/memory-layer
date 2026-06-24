from __future__ import annotations

from typing import Any

from app.db import get_store


def list_task_runs(
    scope: str | None = None,
    outcome: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent task_run records, newest first.

    Args:
        scope: Optional scope filter (e.g. 'project:memory-layer').
        outcome: Optional outcome filter: 'success', 'partial', or 'failed'.
        limit: Maximum results. Clamped to 1–50. Default 10.
    """
    clamped_limit = max(1, min(int(limit), 50))
    return get_store().list_task_runs_db(scope=scope, outcome=outcome, limit=clamped_limit)
