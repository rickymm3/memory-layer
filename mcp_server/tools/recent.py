from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config


def get_recent_memories(
    limit: int = 10,
    scope: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recently stored memory atoms, newest first.

    Queries memory_atoms only. No embeddings are generated; results are ordered
    by created_at DESC. All inputs are validated and passed as parameterized
    query parameters; no user input is interpolated into SQL.

    Each result contains both `content` (full canonical sentence) and
    `context_summary` (compact, prompt-friendly version). Prefer
    `context_summary` for prompt injection and display; use `content` only
    when exact canonical wording matters.

    Args:
        limit: Maximum results to return. Clamped to 1–50. Default 10.
        scope: Optional scope to filter by (e.g. 'project:memory-layer').
                If omitted, returns atoms across all scopes.
    """
    clamped_limit = max(1, min(int(limit), 50))

    config = get_config()

    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            if scope is not None:
                cur.execute(
                    """
                    SELECT
                        id,
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        created_at,
                        support_weight,
                        opposition_weight,
                        disagreement_score,
                        last_recomputed_at,
                        lifecycle_status,
                        superseded_by_atom_id,
                        lifecycle_reason,
                        retrieval_priority,
                        lifecycle_updated_at
                    FROM memory_atoms
                    WHERE scope = %s
                      AND lifecycle_status != 'archived'
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (scope, clamped_limit),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        id,
                        content,
                        context_summary,
                        memory_type,
                        scope,
                        confidence,
                        importance,
                        created_at,
                        support_weight,
                        opposition_weight,
                        disagreement_score,
                        last_recomputed_at,
                        lifecycle_status,
                        superseded_by_atom_id,
                        lifecycle_reason,
                        retrieval_priority,
                        lifecycle_updated_at
                    FROM memory_atoms
                    WHERE lifecycle_status != 'archived'
                    ORDER BY created_at DESC
                    LIMIT %s;
                    """,
                    (clamped_limit,),
                )
            rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        disagreement_score = float(row[10])
        results.append(
            {
                "id": str(row[0]),
                "content": row[1],
                "context_summary": row[2],
                "memory_type": row[3],
                "scope": row[4],
                "confidence": float(row[5]),
                "importance": float(row[6]),
                "created_at": row[7].isoformat() if row[7] else None,
                "support_weight": float(row[8]),
                "opposition_weight": float(row[9]),
                "disagreement_score": disagreement_score,
                "last_recomputed_at": row[11].isoformat() if row[11] else None,
                "disagreement_flag": disagreement_score >= 0.5,
                "lifecycle_status": row[12],
                "superseded_by_atom_id": str(row[13]) if row[13] else None,
                "lifecycle_reason": row[14],
                "retrieval_priority": float(row[15]),
                "lifecycle_updated_at": row[16].isoformat() if row[16] else None,
            }
        )
    return results
