from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config


def get_atom_signals(
    memory_atom_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return signals linked to a memory atom, ordered newest first.

    Use this to inspect provenance and understand why an atom has its current
    aggregated confidence, support_weight, and disagreement_score. Each signal
    represents a single evidence record that contributed to the atom's state.

    Args:
        memory_atom_id: UUID of the memory atom to inspect.
        limit: Maximum number of signals to return. Clamped to 1–100. Default 20.

    Returns:
        List of signal dicts ordered by created_at DESC. Returns an empty list
        if the atom has no linked signals or does not exist. raw_input is
        excluded from output.
    """
    clamped_limit = max(1, min(int(limit), 100))
    config = get_config()

    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    memory_atom_id,
                    parent_signal_id,
                    source_key,
                    source_type,
                    source_id,
                    content,
                    context_summary,
                    memory_type,
                    scope,
                    subject,
                    stance,
                    relationship,
                    certainty,
                    intensity,
                    confidence,
                    importance,
                    reconciliation_reason,
                    created_at
                FROM memory_signals
                WHERE memory_atom_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (memory_atom_id, clamped_limit),
            )
            rows = cur.fetchall()

    results: list[dict[str, Any]] = []
    for row in rows:
        results.append(
            {
                "id": str(row[0]),
                "memory_atom_id": str(row[1]) if row[1] else None,
                "parent_signal_id": str(row[2]) if row[2] else None,
                "source_key": row[3],
                "source_type": row[4],
                "source_id": row[5],
                "content": row[6],
                "context_summary": row[7],
                "memory_type": row[8],
                "scope": row[9],
                "subject": row[10],
                "stance": row[11],
                "relationship": row[12],
                "certainty": float(row[13]) if row[13] is not None else None,
                "intensity": float(row[14]) if row[14] is not None else None,
                "confidence": float(row[15]) if row[15] is not None else None,
                "importance": float(row[16]) if row[16] is not None else None,
                "reconciliation_reason": row[17],
                "created_at": row[18].isoformat() if row[18] else None,
            }
        )
    return results


def fetch_signals_summary_batch(
    conn: psycopg.Connection,
    atom_ids: list[str],
) -> "dict[str, dict[str, Any]]":
    """Return a compact signals summary for a batch of atom IDs.

    Runs two queries against the open *conn* (no new connection opened).
    Returns a dict mapping atom_id → summary with keys:
      count (int), top_sources (list[str], up to 3 by recency), most_recent_signal_at (str|None).
    Atoms with no signals are absent from the result; callers should use
    .get(atom_id, {"count": 0, "top_sources": [], "most_recent_signal_at": None}).
    """
    if not atom_ids:
        return {}

    result: dict[str, dict[str, Any]] = {}

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT memory_atom_id::text, COUNT(*), MAX(created_at)
            FROM memory_signals
            WHERE memory_atom_id = ANY(%s::uuid[])
            GROUP BY memory_atom_id;
            """,
            (atom_ids,),
        )
        for row in cur.fetchall():
            result[row[0]] = {
                "count": int(row[1]),
                "most_recent_signal_at": row[2].isoformat() if row[2] else None,
                "top_sources": [],
            }

    if not result:
        return result

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT atom_id, source_key
            FROM (
                SELECT
                    memory_atom_id::text AS atom_id,
                    source_key,
                    ROW_NUMBER() OVER (
                        PARTITION BY memory_atom_id
                        ORDER BY MAX(created_at) DESC NULLS LAST
                    ) AS rn
                FROM memory_signals
                WHERE memory_atom_id = ANY(%s::uuid[])
                GROUP BY memory_atom_id, source_key
            ) sub
            WHERE rn <= 3
            ORDER BY atom_id, rn;
            """,
            (atom_ids,),
        )
        for row in cur.fetchall():
            atom_id_str, src = row[0], row[1]
            if atom_id_str in result:
                result[atom_id_str]["top_sources"].append(src)

    return result
