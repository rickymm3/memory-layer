from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config


def get_project_context(
    scope: str,
    limit: int = 10,
    min_importance: float = 0.6,
    min_confidence: float = 0.7,
) -> list[dict[str, Any]]:
    """Return high-importance, high-confidence memory atoms for a given scope.

    Queries memory_atoms only. No embeddings are generated; results are ordered
    by importance DESC, confidence DESC, created_at DESC. All inputs are
    validated and passed as parameterized query parameters; no user input is
    interpolated into SQL.

    Each result contains both `content` (full canonical sentence) and
    `context_summary` (compact, prompt-friendly version). Prefer
    `context_summary` for prompt injection and display; use `content` only
    when exact canonical wording matters.

    Args:
        scope: Required. Scope to filter by (e.g. 'project:memory-layer').
        limit: Maximum results to return. Clamped to 1–30. Default 10.
        min_importance: Minimum importance threshold. Clamped to 0.0–1.0. Default 0.6.
        min_confidence: Minimum confidence threshold. Clamped to 0.0–1.0. Default 0.7.
    """
    clamped_limit = max(1, min(int(limit), 30))
    clamped_importance = max(0.0, min(float(min_importance), 1.0))
    clamped_confidence = max(0.0, min(float(min_confidence), 1.0))

    config = get_config()

    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
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
                  AND importance >= %s
                  AND confidence >= %s
                  AND lifecycle_status = 'active'
                ORDER BY importance DESC, confidence DESC, created_at DESC
                LIMIT %s;
                """,
                (scope, clamped_importance, clamped_confidence, clamped_limit),
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
