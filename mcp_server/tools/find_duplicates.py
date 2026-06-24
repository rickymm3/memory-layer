from __future__ import annotations

from typing import Any

from app.db import get_store


def find_duplicate_atoms(
    similarity_threshold: float = 0.90,
    scope: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find pairs of active atoms with very high semantic similarity — candidates for consolidation.

    As a memory corpus grows, near-identical atoms accumulate: the same fact stored
    multiple times with slightly different wording. This tool surfaces those pairs so
    they can be reviewed and merged.

    Implementation notes:
    - SQLite backend: pairwise Python cosine comparison, capped at 500 most-recent atoms.
    - Postgres backend: pgvector distance join (efficient, no Python-side limit).
    - Results are sorted by similarity descending (most redundant pairs first).

    Args:
        similarity_threshold: Cosine similarity cutoff. 0.90 catches near-verbatim duplicates;
            0.80 catches topically-redundant atoms. Default 0.90.
        scope: Restrict to a specific scope. If omitted, searches all scopes.
        limit: Maximum pairs to return. Clamped to 1–100. Default 20.

    Returns:
        Dict with `pairs` list and `total_found` count. Each pair has:
        - `similarity`: cosine similarity between the two atoms
        - `atom_a`, `atom_b`: atom summaries (id, content, memory_type, scope, confidence, created_at)
    """
    clamped_limit = max(1, min(int(limit), 100))
    clamped_threshold = max(0.5, min(1.0, float(similarity_threshold)))

    pairs = get_store().find_near_duplicate_pairs(
        similarity_threshold=clamped_threshold,
        scope=scope,
        limit=clamped_limit,
    )

    return {
        "pairs": pairs,
        "total_found": len(pairs),
        "similarity_threshold": clamped_threshold,
        "note": (
            f"Found {len(pairs)} near-duplicate pair(s) above {clamped_threshold:.2f} similarity. "
            "Review each pair and use memory_update or lifecycle changes to consolidate."
        ) if pairs else (
            f"No near-duplicate pairs found above {clamped_threshold:.2f} similarity threshold."
        ),
    }
