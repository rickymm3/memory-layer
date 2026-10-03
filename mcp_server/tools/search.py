from __future__ import annotations

from typing import Any

from app.db import get_store
from app.retrieval_policy import MIN_SIMILARITY_DEFAULT
from mcp_server.auth_context import current_user_id


def search_memories(
    query: str,
    limit: int = 5,
    scope: str | None = None,
    memory_type: str | None = None,
    min_similarity: float = MIN_SIMILARITY_DEFAULT,
) -> list[dict[str, Any]]:
    """Search memory atoms by semantic similarity with optional filters.

    In SSE/hosted mode (Bearer token present), returns only atoms the
    authenticated user can read: their own, public atoms, and team atoms in
    scopes they belong to. In stdio/local mode, returns all.

    Args:
        min_similarity: Minimum cosine similarity (0.0–1.0). Default 0.35.
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
