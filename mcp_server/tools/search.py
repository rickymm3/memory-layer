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


def search_approved_memories(
    query: str,
    scope: str,
    limit: int = 8,
    memory_type: str | None = None,
    min_similarity: float = 0.45,
    min_confidence: float = 0.70,
    max_disagreement: float = 0.35,
) -> dict[str, Any]:
    """Search only human-approved, active, public atoms within one project scope.

    This is the constrained read path for public knowledge consumers. It never
    treats confidence, visibility, or lifecycle state as a substitute for
    editorial approval.
    """
    normalized_scope = (scope or "").strip()
    if not normalized_scope.startswith("project:"):
        return {
            "scope": normalized_scope,
            "memory_revision": None,
            "count": 0,
            "results": [],
            "error": "scope must be an explicit project:<name> scope",
        }

    store = get_store()
    clamped_limit = max(1, min(int(limit), 20))
    results = store.search_memories_full(
        query=query,
        limit=clamped_limit,
        scope=normalized_scope,
        memory_type=memory_type,
        min_similarity=max(0.0, min(float(min_similarity), 1.0)),
        requesting_user=current_user_id.get(),
        lifecycle_status="active",
        authority_status="approved",
        min_confidence=max(0.0, min(float(min_confidence), 1.0)),
        max_disagreement=max(0.0, min(float(max_disagreement), 1.0)),
        visibility="public",
    )
    return {
        "scope": normalized_scope,
        "memory_revision": store.get_approved_memory_revision(normalized_scope),
        "count": len(results),
        "results": results,
    }
