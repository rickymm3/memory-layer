from __future__ import annotations

from typing import Any

from app.llm_provider import get_llm_client
from app.db import get_store
from app.reconciliation import CandidateReconciler


def reconcile_candidate(
    content: str,
    memory_type: str = "fact",
    scope: str | None = None,
    retrieve_limit: int = 5,
) -> dict[str, Any]:
    """Run the reconciler against existing atoms for a single candidate.

    Compares the candidate against stored memory atoms and classifies the
    relationship. Does not store anything. Use this after
    memory_extract_candidates to decide whether to call memory_store_auto
    or memory_propose_signal.

    Returns:
        relationship: one of new, refinement, duplicate, reinforcement,
                      conflict, opinion_change
        reason: the reconciler's explanation
        matched_memory_ids: UUIDs of related existing atoms
        recommended_action: store_new, ask_user, update_existing, or skip
        is_auto_storable: true when relationship is new/refinement and
                          recommended_action is store_new

    Args:
        content: Full canonical sentence of the candidate to reconcile.
        memory_type: Memory type (fact, decision, instruction, etc.). Default fact.
        scope: Optional scope string. Should match the candidate's intended scope.
        retrieve_limit: How many related atoms to retrieve for comparison.
                        Clamped to 1-10. Default 5.
    """
    clamped_limit = max(1, min(int(retrieve_limit), 10))

    store = get_store()
    reconciler = CandidateReconciler(store=store, ollama=get_llm_client())

    result = reconciler.reconcile(
        content=content,
        memory_type=memory_type,
        scope=scope,
        retrieve_limit=clamped_limit,
    )

    relationship = result.get("relationship", "new")
    recommended_action = result.get("recommended_action", "ask_user")

    return {
        "relationship": relationship,
        "reason": result.get("reason", ""),
        "matched_memory_ids": result.get("matched_memory_ids", []),
        "recommended_action": recommended_action,
        "is_auto_storable": (
            relationship in {"new", "refinement"}
            and recommended_action == "store_new"
        ),
    }
