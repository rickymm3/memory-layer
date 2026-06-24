from __future__ import annotations

from typing import Any

from app.db import get_store


def get_atom_signals(
    memory_atom_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return signals linked to a memory atom, ordered newest first.

    Args:
        memory_atom_id: UUID of the memory atom to inspect.
        limit: Maximum signals to return. Clamped to 1–100. Default 20.
    """
    clamped_limit = max(1, min(int(limit), 100))
    return get_store().get_atom_signals_db(memory_atom_id, limit=clamped_limit)


def fetch_signals_summary_batch(conn: Any, atom_ids: list[str]) -> "dict[str, dict[str, Any]]":
    """Compatibility shim — kept for any callers that pass a psycopg connection.

    New code should call store.get_atom_with_signals() or store.search_memories_full().
    """
    if not atom_ids:
        return {}
    # Detect backend by connection type
    try:
        import psycopg as _psycopg
        if isinstance(conn, _psycopg.Connection):
            return _fetch_pg(conn, atom_ids)
    except ImportError:
        pass
    return {}


def _fetch_pg(conn: Any, atom_ids: list[str]) -> "dict[str, dict[str, Any]]":
    result: dict[str, Any] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT memory_atom_id::text, COUNT(*), MAX(created_at) "
            "FROM memory_signals WHERE memory_atom_id = ANY(%s::uuid[]) "
            "GROUP BY memory_atom_id;",
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
            SELECT atom_id, source_key FROM (
                SELECT memory_atom_id::text AS atom_id, source_key,
                       ROW_NUMBER() OVER (PARTITION BY memory_atom_id
                           ORDER BY MAX(created_at) DESC NULLS LAST) AS rn
                FROM memory_signals WHERE memory_atom_id = ANY(%s::uuid[])
                GROUP BY memory_atom_id, source_key
            ) sub WHERE rn <= 3 ORDER BY atom_id, rn;
            """,
            (atom_ids,),
        )
        for row in cur.fetchall():
            if row[0] in result:
                result[row[0]]["top_sources"].append(row[1])
    return result
