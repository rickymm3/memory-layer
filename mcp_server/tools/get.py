from __future__ import annotations

from typing import Any

from app.db import get_store


def get_memory_by_id(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by its UUID, including signals summary.

    Returns the atom as a dict, or None if not found.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
    return get_store().get_atom_with_signals(memory_id)
