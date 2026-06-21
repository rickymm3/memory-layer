from __future__ import annotations

from typing import Any

from app.commit_pipeline import MemoryCommitPipeline
from app.db import get_store
from app.write_quality import score_write_quality


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
    # ── Write quality pre-gate ────────────────────────────────────────────────
    quality = score_write_quality(content, memory_type=memory_type, stated_importance=importance)
    if quality.decision == "reject":
        return {
            "stored": False,
            "write_action": "rejected_by_quality_gate",
            "decision": "rejected",
            "memory_atom_id": None,
            "memory_signal_id": None,
            "proposal_id": None,
            "content": content,
            "memory_type": memory_type,
            "scope": scope,
            "rejection_reason": f"write quality too low ({quality.quality_score:.2f}): "
                                 + "; ".join(quality.signals),
            "critic_notes": [],
            "quality_score": quality.quality_score,
            "quality_signals": quality.signals,
        }
    # Downgrade: cap importance at quality_score if scorer recommends it
    effective_importance = (
        quality.adjusted_importance
        if quality.adjusted_importance is not None
        else importance
    )

    candidate = {
        "content": content,
        "memory_type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "importance": effective_importance,
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
        "quality_score": quality.quality_score,
        "quality_signals": quality.signals,
    }
