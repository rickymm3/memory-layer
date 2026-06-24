from __future__ import annotations

from typing import Any

from app.db import get_store


def get_recent_memories(
    limit: int = 10,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently stored memory atoms, newest first.

    Args:
        limit: Maximum results to return. Clamped to 1–50. Default 10.
        scope: Optional scope filter. If omitted, returns atoms across all scopes.
    """
    clamped_limit = max(1, min(int(limit), 50))
    return get_store().list_recent(limit=clamped_limit, scope=scope)
