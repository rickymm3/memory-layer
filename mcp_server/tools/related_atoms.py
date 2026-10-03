"""MCP tool: memory_related — traverse the atom relations graph."""
from __future__ import annotations

from app.db import get_store
from mcp_server.auth_context import current_user_id


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
    store = get_store()
    user = current_user_id.get()
    if not store.readable_atom_ids([atom_id], user):
        return {"atom_id": atom_id, "depth": depth, "neighbor_count": 0, "neighbors": []}
    neighbors = store.get_related_atoms(
        atom_id=atom_id,
        depth=max(1, min(int(depth), 3)),
        relation_types=relation_types,
    )
    readable = store.readable_atom_ids([n.get("id") for n in neighbors], user)
    neighbors = [n for n in neighbors if str(n.get("id")) in readable]
    return {
        "atom_id": atom_id,
        "depth": depth,
        "neighbor_count": len(neighbors),
        "neighbors": neighbors,
    }
