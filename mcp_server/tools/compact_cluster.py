"""memory_compact_cluster MCP tool.

Called by the LLM after it has evaluated a cluster of similar atoms and decided
they express the same underlying belief. The LLM supplies:
  - eligible_atom_ids: atoms above the weight threshold → become 'evidence'
  - auto_deprecate_atom_ids: atoms below the threshold → become 'deprecated'
  - belief_content: the canonical belief, anchored in the highest-weighted
    atom's wording and enriched by the others

The tool creates the belief atom, transitions the cluster, writes
supports_belief edges, and logs each event to belief_revision_log.
"""
from __future__ import annotations

from app.db import get_store


async def compact_cluster(
    eligible_atom_ids: list[str],
    belief_content: str,
    synthesis_reason: str,
    auto_deprecate_atom_ids: list[str] | None = None,
    scope: str | None = None,
) -> dict:
    """Compact a cluster of semantically similar atoms into a canonical belief.

    Args:
        eligible_atom_ids: IDs of atoms above the weight threshold. These
            become 'evidence' and are linked to the belief via supports_belief.
        belief_content: The synthesised canonical belief. Must be anchored in
            the wording of the highest-weighted eligible atom and fully
            self-contained (no session-internal jargon).
        synthesis_reason: Why these atoms were compacted — stored in
            lifecycle_reason on each atom and belief_revision_log.
        auto_deprecate_atom_ids: IDs of atoms below the weight threshold.
            These are deprecated directly without entering the belief's
            evidence set.
        scope: Scope for the new belief atom. Defaults to the scope of the
            highest-confidence eligible atom.

    Returns:
        {
            belief_atom_id: str,
            evidence_count: int,
            deprecated_count: int,
            relation_ids: list[str]
        }
    """
    store = get_store()

    if not eligible_atom_ids:
        return {"error": "eligible_atom_ids must contain at least one atom ID"}

    if not belief_content or not belief_content.strip():
        return {"error": "belief_content must not be empty"}

    try:
        result = store.compact_atoms_to_belief(
            eligible_ids=eligible_atom_ids,
            auto_deprecate_ids=auto_deprecate_atom_ids or [],
            belief_content=belief_content.strip(),
            scope=scope,
            synthesis_reason=synthesis_reason,
        )
        return result
    except Exception as exc:
        return {"error": str(exc)}
