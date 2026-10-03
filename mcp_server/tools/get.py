from __future__ import annotations

from typing import Any

from app.db import get_store
from mcp_server.auth_context import current_user_id


def get_memory_by_id(memory_id: str) -> dict[str, Any] | None:
    """Fetch a single memory atom by its UUID, including signals summary.

    Returns the atom as a dict, or None if not found or not readable by the
    authenticated user.

    Args:
        memory_id: UUID string of the memory atom to fetch.
    """
    store = get_store()
    if not store.readable_atom_ids([memory_id], current_user_id.get()):
        return None
    return store.get_atom_with_signals(memory_id)
