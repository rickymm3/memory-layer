from __future__ import annotations

from typing import Any

from app.memory_store import MemoryStore


def propose_memory_signal(
    content: str,
    memory_type: str,
    relationship: str,
    context_summary: str | None = None,
    scope: str | None = None,
    confidence: float = 0.8,
    importance: float = 0.5,
    reconciliation_reason: str | None = None,
    matched_memory_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Queue a confirmation-path candidate for human review and return a proposal ID.

    Writes the candidate to memory_proposals with status='pending_review'.
    Does NOT write to memory_atoms or memory_signals — no memory is stored until
    the CLI review step approves the proposal and memory_store_approved is called.

    Delegates to MemoryStore.store_proposal — single source of truth for
    proposal writes shared with the commit pipeline.

    Args:
        content: Full canonical sentence of the candidate.
        memory_type: Memory type (fact, decision, instruction, etc.).
        relationship: Reconciler relationship (conflict, opinion_change, etc.).
        context_summary: Compact prompt-friendly summary. Defaults to content.
        scope: Optional scope string.
        confidence: Confidence float 0.0–1.0. Default 0.8.
        importance: Importance float 0.0–1.0. Default 0.5.
        reconciliation_reason: Reconciler's reason string, if any.
        matched_memory_ids: Related existing atom UUIDs from reconciliation.
    """
    if not content or not content.strip():
        return {"error": "content must not be empty"}
    if not memory_type or not memory_type.strip():
        return {"error": "memory_type must not be empty"}

    try:
        proposal_id = MemoryStore().store_proposal(
            content=content,
            memory_type=memory_type,
            relationship=relationship,
            context_summary=context_summary,
            scope=scope,
            confidence=confidence,
            importance=importance,
            reconciliation_reason=reconciliation_reason,
            matched_memory_ids=matched_memory_ids,
        )
        return {"proposal_id": proposal_id, "status": "pending_review"}
    except Exception as exc:
        return {"error": f"failed to queue proposal: {exc}"}

