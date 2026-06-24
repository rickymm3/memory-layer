from __future__ import annotations

from typing import Any

from app.db import get_store


def get_project_context(
    scope: str,
    limit: int = 10,
    min_importance: float = 0.6,
    min_confidence: float = 0.7,
) -> list[dict[str, Any]]:
    """Return high-importance, high-confidence memory atoms for a given scope.

    Args:
        scope: Required. Scope to filter by (e.g. 'project:memory-layer').
        limit: Maximum results. Clamped to 1–30. Default 10.
        min_importance: Minimum importance threshold. Default 0.6.
        min_confidence: Minimum confidence threshold. Default 0.7.
    """
    clamped_limit = max(1, min(int(limit), 30))
    clamped_importance = max(0.0, min(float(min_importance), 1.0))
    clamped_confidence = max(0.0, min(float(min_confidence), 1.0))

    return get_store().project_context_atoms(
        scope=scope,
        limit=clamped_limit,
        min_importance=clamped_importance,
        min_confidence=clamped_confidence,
    )
