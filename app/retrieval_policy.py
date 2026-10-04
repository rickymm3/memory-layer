"""Shared retrieval policy: ranking weights, access rule, and conflict thresholds.

One place for the numbers every read path uses, so the Postgres store, the
SQLite store, and the MCP tools rank and filter the same way.
"""
from __future__ import annotations

from typing import Any

# An atom is reported as a disagreement (both sides returned) when it is
# contested or its disagreement_score is above this.
CONFLICT_THRESHOLD: float = 0.4

# Default similarity floor for MCP search. Below this, neighbours are noise
# that crowds the context window.
MIN_SIMILARITY_DEFAULT: float = 0.35

# Composite rank weights. importance and retrieval_priority are stored on every
# atom; ranking now uses them instead of similarity + confidence alone.
W_SIMILARITY = 0.50
W_CONFIDENCE = 0.15
W_IMPORTANCE = 0.15
W_PRIORITY = 0.10
W_DISAGREEMENT = 0.10

# Types whose relevance decays with age (90-day half-life, floored at 0.3).
# Facts, decisions, instructions and constraints do not decay.
VOLATILE_TYPES: tuple[str, ...] = ("opinion", "preference", "lesson", "belief", "observation")

COMPOSITE_SQL = f"""
    (
        similarity * {W_SIMILARITY}
        + confidence * {W_CONFIDENCE}
        + importance * {W_IMPORTANCE}
        + LEAST(COALESCE(retrieval_priority, 1.0), 1.0) * {W_PRIORITY}
        - COALESCE(disagreement_score, 0.0) * {W_DISAGREEMENT}
    ) * CASE
        WHEN memory_type IN ({", ".join(f"'{t}'" for t in VOLATILE_TYPES)})
        THEN GREATEST(0.3, EXP(
            -LN(2) * EXTRACT(EPOCH FROM (NOW() - created_at)) / (90.0 * 86400)
        ))
        ELSE 1.0
    END
"""


def composite_score(
    similarity: float,
    confidence: float,
    importance: float,
    retrieval_priority: float,
    disagreement_score: float,
) -> float:
    """Python twin of COMPOSITE_SQL without the decay factor."""
    return (
        similarity * W_SIMILARITY
        + confidence * W_CONFIDENCE
        + importance * W_IMPORTANCE
        + min(retrieval_priority, 1.0) * W_PRIORITY
        - disagreement_score * W_DISAGREEMENT
    )


def access_clause(requesting_user: str | None, alias: str = "") -> tuple[str, tuple]:
    """SQL predicate for what requesting_user may read.

    public → everyone; private → its owner; team → its owner plus users listed
    in scope_members for the atom's scope. With no requesting user (local stdio
    mode) there is no filter: the caller owns the whole database.
    """
    if not requesting_user:
        return "", ()
    p = f"{alias}." if alias else ""
    sql = (
        f"({p}visibility = 'public'"
        f" OR {p}source_user_id = %s"
        f" OR ({p}visibility = 'team' AND {p}scope IN"
        f" (SELECT scope FROM scope_members WHERE user_id = %s)))"
    )
    return sql, (requesting_user, requesting_user)


def is_conflicted(atom: dict[str, Any]) -> bool:
    return (
        (atom.get("lifecycle_status") or "") == "contested"
        or float(atom.get("disagreement_score") or 0.0) > CONFLICT_THRESHOLD
    )


def format_conflict_lines(atom: dict[str, Any]) -> list[str]:
    """Render an atom's attributed claims as prompt lines, one per claim."""
    conflict = atom.get("conflict") or {}
    lines: list[str] = []
    for side, label in (("supporting", "for"), ("opposing", "against")):
        for claim in conflict.get(side, []):
            when = (claim.get("created_at") or "")[:10]
            who = claim.get("source") or "unknown"
            lines.append(f"  - {who} ({label}, {when}): {claim.get('content', '')}")
    return lines


def attach_conflicts(store: Any, atoms: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a 'conflict' key to every conflicted atom in place.

    A disagreement is reported as the claims themselves, with who made them,
    so the reader sees both sides rather than one hedged atom.
    """
    flagged = [a for a in atoms if is_conflicted(a)]
    if not flagged:
        return atoms
    try:
        claims = store.get_conflict_claims([str(a["id"]) for a in flagged])
    except Exception:
        return atoms  # conflict detail must never block retrieval
    for a in flagged:
        sides = claims.get(str(a["id"]), {"supporting": [], "opposing": []})
        a["conflict"] = {
            "status": "contested",
            "instruction": "Do not treat this as settled. Report each claim with who made it.",
            **sides,
        }
    return atoms
