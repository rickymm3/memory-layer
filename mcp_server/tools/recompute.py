from __future__ import annotations

from typing import Any

from app.db import get_store


def recompute_memory_atom(atom_id: str) -> dict[str, Any]:
    """Recompute signal-aggregation weights for a single memory atom.

    Fetches all linked memory_signals for the atom, recomputes
    support_weight, opposition_weight, disagreement_score, and confidence
    from the signal history, and persists the results to memory_atoms.

    Call this after CLI-path writes (memory_store_approved) or after bulk
    recomputation via `make recompute-weights` to refresh aggregation fields.

    Args:
        atom_id: UUID of the memory atom to recompute.

    Returns:
        Updated atom dict including all aggregation fields on success.
        {"error": "not found", "atom_id": "<uuid>"} if the atom does not exist.
    """
    store = get_store()
    result = store.recompute_atom_weights(atom_id)
    if result is None:
        return {"error": "not found", "atom_id": atom_id}
    return result
