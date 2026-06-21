"""MCP tool: memory_link_atoms — create an explicit relation between two atoms."""
from __future__ import annotations

from app.db import get_store


def link_atoms(
    atom_a_id: str,
    atom_b_id: str,
    relation_type: str = "related",
    confidence: float = 0.8,
    source_key: str = "local_user",
) -> dict:
    """Create a directed relation edge from atom_a_id to atom_b_id.

    Valid relation_types: supports, contradicts, specializes, generalizes, related.
    Returns the relation id on success.
    """
    try:
        rel_id = get_store().link_atoms(
            atom_a_id=atom_a_id,
            atom_b_id=atom_b_id,
            relation_type=relation_type,
            confidence=float(confidence),
            source_key=source_key,
        )
        return {
            "status": "linked",
            "relation_id": rel_id,
            "atom_a_id": atom_a_id,
            "atom_b_id": atom_b_id,
            "relation_type": relation_type,
            "confidence": confidence,
        }
    except ValueError as e:
        return {"status": "error", "message": str(e)}
