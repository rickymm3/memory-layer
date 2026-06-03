"""Shared post-turn reflection logic.

Both the local chat pipeline (app/chat.py) and the MCP transport layer
(mcp_server/tools/reflect_turn.py) call run_turn_reflection() — one source
of truth for "extract candidates from a turn and commit them via the pipeline."
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def run_turn_reflection(
    user_msg: str,
    thinking: str,
    answer: str,
    scope: str | None = None,
) -> dict[str, Any]:
    """Extract durable insights from a conversation turn and commit them.

    Passes (user message + LLM reasoning + LLM answer) through
    extract_candidates_from_turn → MemoryCommitPipeline.  Each candidate that
    the extraction LLM marks should_store=True is evaluated by the reconciler,
    critic, and risk gate before anything is written.

    Args:
        user_msg: The user's message for this turn.
        thinking: Raw chain-of-thought text from <think> blocks, if any.
        answer: The assistant's final answer (think blocks already stripped).
        scope: Optional scope override applied to all committed atoms.

    Returns:
        {
            "committed": [{"atom_id", "final_memory_text", "memory_type",
                           "scope", "write_action"}, ...],
            "proposed":  [{"candidate_text", "proposal_id", "reason"}, ...],
            "skipped":   <int>,
            "error":     "<str>"  # only present on extraction failure
        }
    """
    from scripts.extract_memory_candidates import extract_candidates_from_turn
    from app.commit_pipeline import MemoryCommitPipeline

    if not user_msg.strip() and not answer.strip():
        return {
            "committed": [],
            "proposed": [],
            "skipped": 0,
            "error": "Both user_msg and answer are empty.",
        }

    try:
        extracted = extract_candidates_from_turn(
            user_msg=user_msg,
            thinking=thinking,
            answer=answer,
            include_rejected=False,
        )
    except Exception as exc:
        _logger.warning("run_turn_reflection: extraction failed: %s", exc)
        return {"committed": [], "proposed": [], "skipped": 0, "error": str(exc)}

    candidates = [
        c for c in extracted.get("candidates", [])
        if isinstance(c, dict) and c.get("should_store")
    ]

    if not candidates:
        return {"committed": [], "proposed": [], "skipped": 0}

    if scope:
        for c in candidates:
            c["scope"] = scope

    pipeline = MemoryCommitPipeline()
    committed: list[dict] = []
    proposed: list[dict] = []
    skipped = 0

    for candidate in candidates:
        try:
            decision = pipeline.commit_candidate(candidate)
            d = decision.to_dict()
            if d.get("write_action") in ("proposed", "mark_conflict", "propose_for_review"):
                proposed.append({
                    "candidate_text": d["candidate_text"],
                    "proposal_id": d.get("proposal_id"),
                    "reason": d.get("rejection_reason"),
                })
            elif d.get("committed_atom_id"):
                committed.append({
                    "atom_id": d["committed_atom_id"],
                    "final_memory_text": d["final_memory_text"],
                    "memory_type": d["memory_type"],
                    "scope": d["scope"],
                    "write_action": d["write_action"],
                })
            else:
                skipped += 1
        except Exception as exc:
            _logger.warning("run_turn_reflection: pipeline failed for candidate: %s", exc)
            skipped += 1

    return {"committed": committed, "proposed": proposed, "skipped": skipped}
