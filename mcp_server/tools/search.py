from __future__ import annotations

from typing import Any

from app.db import get_store
from mcp_server.auth_context import current_user_id


def search_memories(
    query: str,
    limit: int = 5,
    scope: str | None = None,
    memory_type: str | None = None,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Search memory atoms by semantic similarity with optional filters.

    In SSE/hosted mode (Bearer token present), returns only atoms owned by
    the authenticated user plus public atoms. In stdio/local mode, returns all.

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
        requesting_user=current_user_id.get(),
    )
