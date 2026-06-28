from __future__ import annotations

from typing import Any

from app.commit_pipeline import MemoryCommitPipeline
from app.db import get_store
from app.write_quality import score_write_quality
from mcp_server.auth_context import current_user_id


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
    source_user_id: str | None = None,
    visibility: str = "public",
) -> dict[str, Any]:
    """Store a candidate through the full commit pipeline and return a write report.

    All candidates pass through the epistemic classifier, then reconciliation,
    critic review, and the risk gate before any write occurs.

    The epistemic classifier NEVER rejects for quality reasons — only for:
      - Secrets/credentials (GUARDRAILS patterns)
      - Empty content
      - Invalid scope format

    Everything else is stored with appropriate type and confidence based on
    the epistemic tier detected (VERIFIED, REPORTED, BELIEVED, OBSERVED).

    Args:
        content: Full canonical sentence of the candidate to store.
        memory_type: Memory type (fact, decision, instruction, belief, etc.).
        relationship: Reconciler output hint (informational; pipeline re-reconciles).
        context_summary: Compact prompt-friendly summary. Defaults to content.
        scope: Optional scope string (e.g. 'project:memory-layer').
        confidence: Confidence float 0.0–1.0. Default 0.8.
        importance: Importance float 0.0–1.0. Default 0.5.
        reconciliation_reason: Reconciler's reason string, if any.
        matched_memory_ids: Related existing atom UUIDs from reconciliation.
    """
    # Fall back to SSE auth context if no explicit source_user_id provided
    if source_user_id is None:
        source_user_id = current_user_id.get()

    # ── Epistemic classifier (replaces old quality gate) ──────────────────────
    quality = score_write_quality(content, memory_type=memory_type, stated_importance=importance, scope=scope)
    if quality.decision == "reject":
        # Hard safety block only — secrets, empty, invalid scope
        return {
            "stored": False,
            "write_action": "rejected_by_guardrail",
            "decision": "rejected",
            "memory_atom_id": None,
            "memory_signal_id": None,
            "proposal_id": None,
            "content": content,
            "memory_type": memory_type,
            "scope": scope,
            "rejection_reason": "; ".join(quality.signals),
            "critic_notes": [],
            "quality_score": quality.quality_score,
            "quality_signals": quality.signals,
        }

    # Use classifier's confidence suggestion if provided (e.g. for belief-tier content)
    effective_confidence = quality.suggested_confidence if quality.suggested_confidence is not None else confidence
    # Cap importance for downgraded content
    effective_importance = (
        quality.adjusted_importance
        if quality.adjusted_importance is not None
        else importance
    )
    # Use classifier's suggested type if caller didn't declare a specific type
    effective_type = (
        quality.suggested_memory_type
        if quality.suggested_memory_type and memory_type in ("fact", "")
        else memory_type
    )

    candidate = {
        "content": content,
        "memory_type": effective_type,
        "scope": scope,
        "confidence": effective_confidence,
        "importance": effective_importance,
        "context_summary": context_summary or "",
        "should_store": True,
        # Hints for the critic LLM (only set when classifier detected something)
        "reframe_hint": quality.reframe_hint,
        "suggested_memory_type": quality.suggested_memory_type,
    }

    try:
        decision = MemoryCommitPipeline().commit_candidate(
            candidate,
            source_user_id=source_user_id,
            visibility=visibility,
        )
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
        "epistemic_tier": (
            "OBSERVED" if scope and scope.startswith("model:") else
            "VERIFIED" if quality.quality_score >= 0.7 else
            "REPORTED" if quality.quality_score >= 0.5 else
            "BELIEVED"
        ),
    }
