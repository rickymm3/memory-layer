from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config
from mcp_server.tools.get_signals import fetch_signals_summary_batch


def get_memory_by_id(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by its UUID.

    Returns the atom as a dict, or None if no atom with that id exists.
    Input is passed as a parameterized query parameter; no user input is
    interpolated into SQL.

    Each result contains both `content` (full canonical sentence) and
    `context_summary` (compact, prompt-friendly version). Prefer
    `context_summary` for prompt injection and display; use `content` only
    when exact canonical wording matters.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
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
                    support_weight,
                    opposition_weight,
                    disagreement_score,
                    last_recomputed_at,
                    created_at,
                    lifecycle_status,
                    superseded_by_atom_id,
                    lifecycle_reason,
                    retrieval_priority,
                    lifecycle_updated_at
                FROM memory_atoms
                WHERE id = %s;
                """,
                (memory_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

        sigs = fetch_signals_summary_batch(conn, [memory_id])

    atom_id_str = str(row[0])
    summary = sigs.get(
        atom_id_str,
        {"count": 0, "top_sources": [], "most_recent_signal_at": None},
    )
    return {
        "id": atom_id_str,
        "content": row[1],
        "context_summary": row[2],
        "memory_type": row[3],
        "scope": row[4],
        "confidence": float(row[5]),
        "importance": float(row[6]),
        "support_weight": float(row[7]),
        "opposition_weight": float(row[8]),
        "disagreement_score": float(row[9]),
        "disagreement_flag": float(row[9]) >= 0.5,
        "last_recomputed_at": row[10].isoformat() if row[10] else None,
        "created_at": row[11].isoformat() if row[11] else None,
        "lifecycle_status": row[12],
        "superseded_by_atom_id": str(row[13]) if row[13] else None,
        "lifecycle_reason": row[14],
        "retrieval_priority": float(row[15]),
        "lifecycle_updated_at": row[16].isoformat() if row[16] else None,
        "signals_summary": summary,
    }
