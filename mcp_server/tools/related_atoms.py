"""MCP tool: memory_related — traverse the atom relations graph."""
from __future__ import annotations

from app.db import get_store


def get_related_atoms(
    atom_id: str,
    depth: int = 1,
    relation_types: list[str] | None = None,
) -> dict:
    """Return atoms related to atom_id up to `depth` hops (max 3).

    Traverses memory_atom_relations bidirectionally. Optionally filter by
    relation_type list: ['supports', 'contradicts', 'specializes',
    'generalizes', 'related'].
    """
    neighbors = get_store().get_related_atoms(
        atom_id=atom_id,
        depth=max(1, min(int(depth), 3)),
        relation_types=relation_types,
    )
    return {
        "atom_id": atom_id,
        "depth": depth,
        "neighbor_count": len(neighbors),
        "neighbors": neighbors,
    }
