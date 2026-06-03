from __future__ import annotations

from typing import Any

from app.commit_pipeline import MemoryCommitPipeline


def store_memory_auto(
    content: str,
    memory_type: str,
    relationship: str,
    context_summary: str | None = None,
    scope: str | None = None,
    confidence: float = 0.8,
    importance: float = 0.5,
    reconciliation_reason: str | None = None,
    matched_memory_ids: list[str] | None = None,
    task_run_id: str | None = None,
) -> dict[str, Any]:
    """Store a candidate through the full commit pipeline and return a write report.

    All candidates pass through reconciliation, critic review, and the risk gate
    before any write occurs — the pipeline decides the final write action.
    Previously 'new' and 'refinement' were auto-stored and 'conflict'/'opinion_change'
    were rejected here; now the pipeline applies those rules uniformly.

    Args:
        content: Full canonical sentence of the candidate to store.
        memory_type: Memory type (fact, decision, instruction, etc.).
        relationship: Reconciler output hint (informational; pipeline re-reconciles).
        context_summary: Compact prompt-friendly summary. Defaults to content.
        scope: Optional scope string (e.g. 'project:memory-layer').
        confidence: Confidence float 0.0–1.0. Default 0.8.
        importance: Importance float 0.0–1.0. Default 0.5.
        reconciliation_reason: Reconciler's reason string, if any.
        matched_memory_ids: Related existing atom UUIDs from reconciliation.
    """
    candidate = {
        "content": content,
        "memory_type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "importance": importance,
        "context_summary": context_summary or "",
        "should_store": True,
    }

    try:
        decision = MemoryCommitPipeline().commit_candidate(candidate)
    except Exception as exc:
        return {"stored": False, "error": str(exc)}

    d = decision.to_dict()
    committed = d.get("committed_atom_id") is not None

    return {
        "stored": committed,
        "write_action": d.get("write_action"),
        "decision": d.get("decision"),
        "memory_atom_id": d.get("committed_atom_id"),
        "memory_signal_id": d.get("committed_signal_id"),
        "proposal_id": d.get("proposal_id"),
        "content": d.get("final_memory_text") or content,
        "memory_type": d.get("memory_type"),
        "scope": d.get("scope"),
        "rejection_reason": d.get("rejection_reason"),
        "critic_notes": d.get("critic_notes", []),
    }
    """Store a low-risk candidate via the auto-store path and return a write report.

    Only valid for relationship values 'new' and 'refinement'. Rejects
    'conflict' and 'opinion_change' — those must go through
    memory_propose_signal. Runs an exact-match guard before writing.
    Writes memory_atom + linked memory_signal in a single transaction.
    Never stores silently — always returns a structured write report.

    Args:
        content: Full canonical sentence of the candidate to store.
        memory_type: Memory type (fact, decision, instruction, etc.).
        relationship: Reconciler output. Must be 'new' or 'refinement'.
        context_summary: Compact prompt-friendly summary. Defaults to content.
        scope: Optional scope string (e.g. 'project:memory-layer').
        confidence: Confidence float 0.0–1.0. Clamped. Default 0.8.
        importance: Importance float 0.0–1.0. Clamped. Default 0.5.
        reconciliation_reason: Reconciler's reason string, if any.
        matched_memory_ids: Related existing atom UUIDs from reconciliation.
    """
    relationship = relationship.strip().lower()

    if relationship in _REJECTED_RELATIONSHIPS:
        return {
            "stored": False,
            "error": (
                f"relationship '{relationship}' requires user confirmation. "
                "Use memory_propose_signal instead."
            ),
        }

    if relationship not in _AUTO_STORABLE_RELATIONSHIPS:
        return {
            "stored": False,
            "error": (
                f"unknown relationship '{relationship}'. "
                "Expected 'new' or 'refinement'."
            ),
        }

    clamped_confidence = max(0.0, min(float(confidence), 1.0))
    clamped_importance = max(0.0, min(float(importance), 1.0))
    summary = (context_summary or "").strip() or content

    store = MemoryStore()

    exact_match = store.find_exact_content_match(content)
    if exact_match:
        return {
            "stored": False,
            "error": "exact duplicate",
            "existing_memory_atom_id": str(exact_match["id"]),
        }

    signal_metadata: dict | None = None
    if matched_memory_ids:
        signal_metadata = {"matched_memory_ids": matched_memory_ids}

    atom_id, signal_id = store.store_memory_with_signal(
        content=content,
        context_summary=summary,
        memory_type=memory_type,
        scope=scope,
        confidence=clamped_confidence,
        importance=clamped_importance,
        relationship=relationship,
        reconciliation_reason=reconciliation_reason,
        signal_metadata=signal_metadata,
        task_run_id=task_run_id,
    )

    return {
        "stored": True,
        "memory_atom_id": atom_id,
        "memory_signal_id": signal_id,
        "content": content,
        "memory_type": memory_type,
        "scope": scope,
        "relationship": relationship,
        "signal_created": True,
        "auto_stored": True,
    }
