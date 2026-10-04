"""Cross-user topic matching: find public atoms from other users semantically similar to a given atom.

Privacy: source identity is anonymized until the user explicitly engages.
Never expose who said what — only the matched topic content.
"""
from __future__ import annotations

from typing import Any

import psycopg

from app.config import get_config
from app.db import get_store

MATCH_THRESHOLD = 0.75   # cosine similarity floor for cross-user matches
MATCH_LIMIT = 5


def find_related_public(
    atom_id: str,
    requesting_username: str,
    threshold: float = MATCH_THRESHOLD,
    limit: int = MATCH_LIMIT,
) -> list[dict[str, Any]]:
    """Return public atoms from OTHER users semantically similar to atom_id.

    Results are anonymized: no usernames or signal metadata are included.
    The 'id' can be used to retrieve full detail only after the user engages.
    """
    store = get_store()
    cfg = get_config()

    # Fetch the source atom's embedding
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT embedding FROM memory_atoms WHERE id = %s AND lifecycle_status = 'active';",
                (atom_id,),
            )
            row = cur.fetchone()
    if not row or row[0] is None:
        return []

    embedding_literal = store._vector_literal(row[0])

    # Search for similar public atoms from other users
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT ma.id, ma.content, ma.memory_type, ma.topic_tags,
                       1 - (ma.embedding <=> {embedding_literal}::vector) AS similarity,
                       ma.confidence
                FROM memory_atoms ma
                JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                WHERE ma.visibility = 'public'
                  AND ma.lifecycle_status = 'active'
                  AND ma.id != %s
                  AND ms.source_user_id != %s
                  AND 1 - (ma.embedding <=> {embedding_literal}::vector) >= %s
                GROUP BY ma.id
                ORDER BY similarity DESC
                LIMIT %s;
                """,
                (atom_id, requesting_username, threshold, limit),
            )
            rows = cur.fetchall()

    return [
        {
            "id": str(r[0]),
            "content": r[1],
            "memory_type": r[2],
            "topic_tags": r[3] or [],
            "similarity": round(float(r[4]), 3),
            "confidence": round(float(r[5]), 3),
            # No source_user_id — anonymized until engagement
        }
        for r in rows
    ]


def find_connections_for_user(
    username: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return a user's public atoms that have cross-user matches above the threshold.

    Used for the /connections dashboard view.
    """
    cfg = get_config()
    store = get_store()

    # Get the user's public atoms
    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ma.id, ma.content, ma.memory_type, ma.confidence
                FROM memory_atoms ma
                JOIN memory_signals ms ON ms.memory_atom_id = ma.id
                WHERE ms.source_user_id = %s
                  AND ma.visibility = 'public'
                  AND ma.lifecycle_status = 'active'
                  AND ma.embedding IS NOT NULL
                LIMIT %s;
                """,
                (username, limit),
            )
            user_atoms = cur.fetchall()

    results = []
    for atom_row in user_atoms:
        atom_id = str(atom_row[0])
        matches = find_related_public(
            atom_id=atom_id,
            requesting_username=username,
            threshold=MATCH_THRESHOLD,
            limit=3,
        )
        if matches:
            results.append({
                "atom_id": atom_id,
                "atom_content": atom_row[1],
                "memory_type": atom_row[2],
                "confidence": round(float(atom_row[3]), 3),
                "matches": matches,
            })

    return results
