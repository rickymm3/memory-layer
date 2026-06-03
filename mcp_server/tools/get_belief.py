from __future__ import annotations

from typing import Any

from app.memory_store import MemoryStore
from app.llm_provider import get_llm_client


def get_belief_detail(
    atom_id: str | None = None,
    query: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Return the full belief picture for a memory atom.

    Combines:
    - The atom (current belief text, confidence, lifecycle status, aggregation weights)
    - Signals split into supporting vs opposing evidence
    - Revision history from birth through every refine/supersede/reinforce event

    Exactly one of atom_id or query must be provided.
    If query is provided, a semantic search is run and the top match is used.

    Args:
        atom_id: UUID of the memory atom to inspect directly.
        query: Natural language question; top semantic match is used.
        scope: Optional scope filter when using query (e.g. 'user', 'project:myapp').

    Returns:
        {
            "atom":             {...},           # current state of the belief
            "signals": {
                "supporting":   [...],           # evidence that reinforces the belief
                "opposing":     [...],           # evidence that challenges it
            },
            "revision_history": [...],           # oldest-first, shows how belief evolved
            "belief_summary":   str,             # plain-English one-liner for prompt use
        }
        or {"error": "<message>"} if nothing is found or both/neither args are provided.
    """
    if atom_id and query:
        return {"error": "Provide either atom_id or query, not both."}
    if not atom_id and not query:
        return {"error": "Provide either atom_id or query."}

    store = MemoryStore(ollama_client=get_llm_client())

    resolved_id: str | None = atom_id

    if query:
        results = store.retrieve_memories(
            query=query,
            limit=1,
            scope_filter=scope,
        )
        if not results:
            return {"error": f"No memory found matching query: {query!r}"}
        resolved_id = results[0]["id"]

    detail = store.get_belief_detail(resolved_id)
    if detail is None:
        return {"error": f"No memory atom found with id: {resolved_id}"}

    # Build a plain-English summary line for quick prompt injection.
    atom = detail["atom"]
    sigs = detail["signals"]
    rev = detail["revision_history"]

    support_count = len(sigs["supporting"])
    oppose_count = len(sigs["opposing"])
    revision_count = len(rev)

    confidence_pct = int(round(atom["confidence"] * 100))
    scope_label = f" [{atom['scope']}]" if atom.get("scope") else ""

    last_event: str | None = None
    if rev:
        last = rev[-1]
        last_event = (
            f"Last {last['event_type']} on {last['revised_at'][:10]}"
            if last.get("revised_at")
            else None
        )

    summary_parts = [
        f"Belief{scope_label}: \"{atom['content'][:120]}\"",
        f"Confidence: {confidence_pct}%",
        f"Support signals: {support_count}, Opposition signals: {oppose_count}",
    ]
    if revision_count > 0:
        summary_parts.append(f"Revisions: {revision_count}")
    if last_event:
        summary_parts.append(last_event)
    if atom.get("lifecycle_status") and atom["lifecycle_status"] != "active":
        summary_parts.append(f"Status: {atom['lifecycle_status']}")

    detail["belief_summary"] = " | ".join(summary_parts)
    return detail
