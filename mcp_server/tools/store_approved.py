from __future__ import annotations

import json
from typing import Any

from app.db import get_store


def store_memory_approved(
    proposal_id: str,
    approval_token: str,
    authority_reviewer: str = "human_review",
) -> dict[str, Any]:
    """Store a confirmation-path candidate after CLI review and approval.

    Looks up the proposal by ID, validates the CLI-issued approval token,
    then writes the memory_atom + memory_signal and marks the proposal as 'used'.

    The approval token is issued by `make review-proposals`. It is single-use
    and expires 5 minutes after issuance.

    Args:
        proposal_id: UUID of the proposal returned by memory_propose_signal.
        approval_token: Short-lived token issued by the CLI review step.
    """
    if not proposal_id or not proposal_id.strip():
        return {"stored": False, "error": "proposal_id must not be empty"}
    if not approval_token or not approval_token.strip():
        return {"stored": False, "error": "approval_token must not be empty"}

    try:
        store = get_store()
        proposal = store.get_and_claim_proposal(proposal_id, approval_token)
        if "error" in proposal:
            return {"stored": False, "error": proposal["error"]}

        matched_ids: list[str] | None = None
        if proposal.get("matched_memory_ids"):
            try:
                matched_ids = json.loads(proposal["matched_memory_ids"])
            except (ValueError, TypeError):
                matched_ids = None

        signal_metadata: dict = {"proposal_id": proposal_id}
        if matched_ids:
            signal_metadata["matched_memory_ids"] = matched_ids

        atom_id, signal_id = store.store_memory_with_signal(
            content=proposal["content"],
            context_summary=proposal.get("context_summary") or proposal["content"],
            memory_type=proposal["memory_type"],
            scope=proposal.get("scope"),
            confidence=float(proposal["confidence"]),
            importance=float(proposal["importance"]),
            relationship=proposal.get("relationship"),
            reconciliation_reason=proposal.get("reconciliation_reason"),
            signal_metadata=signal_metadata,
            source_key="human_review",
            source_type="human_review",
            atom_source_type="reviewed_proposal",
            authority_status="approved",
            authority_reviewer=authority_reviewer,
            visibility=proposal.get("visibility") or "private",
            source_user_id=proposal.get("source_user_id"),
        )

        return {
            "stored": True,
            "memory_atom_id": atom_id,
            "memory_signal_id": signal_id,
            "content": proposal["content"],
            "memory_type": proposal["memory_type"],
            "scope": proposal.get("scope"),
            "relationship": proposal.get("relationship"),
            "signal_created": True,
            "auto_stored": False,
            "authority_status": "approved",
        }

    except Exception as exc:
        return {"stored": False, "error": f"store_memory_approved failed: {exc}"}
