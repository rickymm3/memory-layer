"""MCP tool: log_turn

Records one AI conversation turn to the audit tables (prompt history).
"""
from __future__ import annotations

import logging
from typing import Any

from app.db import get_store

_logger = logging.getLogger(__name__)

_VALID_CONTEXT_STATUSES = frozenset({
    "sufficient", "insufficient", "stale", "conflicting",
    "unsupported", "needs_verification",
    "needs_user_clarification", "unsafe_or_blocked",
})
_VALID_VERDICTS = frozenset({
    "approved", "needs_caveat", "needs_revision", "needs_verification", "blocked",
})
_ACTION_MAP = {
    "sufficient": "answer",
    "needs_verification": "verify_then_answer",
    "conflicting": "answer_with_caveat",
    "stale": "answer_with_caveat",
    "needs_user_clarification": "ask_user",
    "unsafe_or_blocked": "refuse",
}


def log_turn(
    user_message: str,
    assistant_response: str,
    retrieved_atom_ids: list[str] | None = None,
    used_atom_ids: list[str] | None = None,
    context_status: str = "sufficient",
    verdict: str = "approved",
    confidence: float = 0.85,
    reasoning: str = "",
    source: str = "mcp_copilot",
    session_id: str | None = None,
    turn_number: int = 0,
) -> dict[str, Any]:
    """Write one conversation turn to the prompt-history audit tables."""
    retrieved = list(retrieved_atom_ids or [])
    used = list(used_atom_ids or [])

    if context_status not in _VALID_CONTEXT_STATUSES:
        context_status = "sufficient"
    if verdict not in _VALID_VERDICTS:
        verdict = "approved"
    confidence = max(0.0, min(1.0, float(confidence)))
    final_action = _ACTION_MAP.get(context_status, "answer")

    store = get_store()
    result = store.log_conversation_turn(
        user_message=user_message,
        assistant_response=assistant_response,
        retrieved_atom_ids=retrieved,
        used_atom_ids=used,
        context_status=context_status,
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        source=source,
        final_action=final_action,
    )

    # Log context size metrics for efficiency tracking (fat atom detection).
    # Look up actual atom content lengths for a realistic token estimate.
    if hasattr(store, "log_context_metrics"):
        try:
            retrieved_atom_tokens = 0
            if retrieved and hasattr(store, "get_memory_by_id"):
                for aid in retrieved:
                    try:
                        atom = store.get_memory_by_id(aid)
                        if atom:
                            retrieved_atom_tokens += len(atom.get("content", "")) // 4
                    except Exception:
                        retrieved_atom_tokens += 9  # fallback: UUID length / 4
            store.log_context_metrics(
                session_id=session_id,
                turn_number=turn_number,
                retrieved_atom_count=len(retrieved),
                used_atom_count=len(used),
                retrieved_atom_tokens=retrieved_atom_tokens,
                user_tokens=len(user_message) // 4,
                assistant_tokens=len(assistant_response) // 4,
            )
        except Exception:
            pass  # never crash log_turn over metrics

    # Auto-link co-used atoms: atoms used together in the same response
    # share implicit context.  Create bidirectional "related" edges between
    # every pair.  Confidence 0.6 < manual links to reflect inferred signal.
    links_created = 0
    if hasattr(store, "link_atoms") and len(used) >= 2:
        for i in range(len(used)):
            for j in range(i + 1, len(used)):
                try:
                    store.link_atoms(
                        used[i], used[j],
                        relation_type="related",
                        confidence=0.6,
                        source_key="auto_coused",
                    )
                    links_created += 1
                except Exception:
                    pass  # never crash log_turn over graph linking

    _logger.info(
        "log_turn: wrote response_trace=%s context_trace=%s source=%s auto_links=%d",
        result.get("response_trace_id"), result.get("context_trace_id"), source, links_created,
    )
    result["auto_links_created"] = links_created
    return result
