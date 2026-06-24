from __future__ import annotations

from typing import Any

from app.db import get_store


def search_memories(
    query: str,
    limit: int = 5,
    scope: str | None = None,
    memory_type: str | None = None,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Search memory atoms by semantic similarity with optional filters.

    Args:
        min_similarity: Minimum cosine similarity (0.0–1.0). Default 0.0.
            Recommended for Claude Code use: 0.45.
    """
    clamped_limit = max(1, min(int(limit), 20))
    return get_store().search_memories_full(
        query=query,
        limit=clamped_limit,
        scope=scope,
        memory_type=memory_type,
        min_similarity=min_similarity,
    )
