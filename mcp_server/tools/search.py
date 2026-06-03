from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config
from app.llm_provider import get_llm_client
from mcp_server.tools.get_signals import fetch_signals_summary_batch


def search_memories(
    query: str,
    limit: int = 5,
    scope: str | None = None,
    memory_type: str | None = None,
    min_similarity: float = 0.0,
) -> list[dict[str, Any]]:
    """Search memory atoms by semantic similarity with optional filters.

    Embeds the query using Ollama and retrieves the most similar atoms via
    cosine similarity. Limit is clamped to 1-20. All filter values are passed
    as parameterized query parameters; no user input is interpolated into SQL.

    Each result contains both `content` (full canonical sentence) and
    `context_summary` (compact, prompt-friendly version). Prefer
    `context_summary` for prompt injection and display; use `content` only
    when exact canonical wording matters.

    Args:
        min_similarity: Minimum cosine similarity threshold (0.0-1.0). Results
            below this score are excluded. Default 0.0 (no filtering).
            Recommended value for Copilot use: 0.45.
    """
    clamped_limit = max(1, min(int(limit), 20))
    clamped_min_similarity = max(0.0, min(float(min_similarity), 1.0))

    config = get_config()
    embedding = get_llm_client().embed_text(query)
    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    # WHERE conditions are hardcoded column names; only values are parameterized
    where_clauses: list[str] = ["lifecycle_status != 'archived'"]
    filter_params: list[Any] = []

    if scope:
        where_clauses.append("scope = %s")
        filter_params.append(scope)
    if memory_type:
        where_clauses.append("memory_type = %s")
        filter_params.append(memory_type)

    where_sql = "WHERE " + " AND ".join(where_clauses)

    # Param order: similarity %s, filter values, order %s, limit %s
    params: list[Any] = [embedding_literal] + filter_params + [embedding_literal, clamped_limit]

    _empty_summary: dict[str, Any] = {"count": 0, "top_sources": [], "most_recent_signal_at": None}

    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT
                    id,
                    content,
                    context_summary,
                    memory_type,
                    scope,
                    confidence,
                    importance,
                    1 - (embedding <=> %s::vector) AS similarity,
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
                {where_sql}
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
                tuple(params),
            )
            rows = cur.fetchall()

        valid_rows: list[tuple[Any, float]] = [
            (row, float(row[7]))
            for row in rows
            if float(row[7]) >= clamped_min_similarity
        ]
        atom_ids = [str(row[0]) for row, _ in valid_rows]
        sigs = fetch_signals_summary_batch(conn, atom_ids)

    results: list[dict[str, Any]] = []
    for row, similarity in valid_rows:
        atom_id_str = str(row[0])
        disagreement_score = float(row[11])
        results.append(
            {
                "id": atom_id_str,
                "content": row[1],
                "context_summary": row[2],
                "memory_type": row[3],
                "scope": row[4],
                "confidence": float(row[5]),
                "importance": float(row[6]),
                "similarity": similarity,
                "created_at": row[8].isoformat() if row[8] is not None else None,
                "support_weight": float(row[9]),
                "opposition_weight": float(row[10]),
                "disagreement_score": disagreement_score,
                "last_recomputed_at": row[12].isoformat() if row[12] is not None else None,
                "disagreement_flag": disagreement_score >= 0.5,
                "lifecycle_status": row[13],
                "superseded_by_atom_id": str(row[14]) if row[14] else None,
                "lifecycle_reason": row[15],
                "retrieval_priority": float(row[16]),
                "lifecycle_updated_at": row[17].isoformat() if row[17] else None,
                "signals_summary": sigs.get(atom_id_str, _empty_summary),
            }
        )

    return results
