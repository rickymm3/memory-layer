"""MCP tool: log_turn

Records one AI conversation turn (user prompt + assistant response + memory context)
into runtime_context_traces + runtime_response_traces so it appears in the
dashboard prompt-history page.

Intended to be called by the AI agent at the end of any turn where memory was
retrieved or stored via MCP tools.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import psycopg

from app.config import get_config

_logger = logging.getLogger(__name__)

# Valid values enforced by DB CHECK constraints.
_VALID_CONTEXT_STATUSES = frozenset({
    "sufficient", "insufficient", "stale", "conflicting",
    "unsupported", "needs_verification",
    "needs_user_clarification", "unsafe_or_blocked",
})
_VALID_FINAL_ACTIONS = frozenset({
    "answer", "answer_with_caveat", "verify_then_answer", "ask_user", "refuse",
})
_VALID_VERDICTS = frozenset({
    "approved", "needs_caveat", "needs_revision", "needs_verification", "blocked",
})


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
) -> dict[str, Any]:
    """Write one conversation turn to the prompt-history audit tables.

    Call this at the end of any turn where you retrieved or stored memories so
    the turn is visible in the dashboard under Prompt History.

    Args:
        user_message:       The user's raw prompt for this turn.
        assistant_response: The final answer you gave the user.
        retrieved_atom_ids: UUIDs of all atoms fetched via memory_search / memory_get
                            during this turn.  Pass [] if none were retrieved.
        used_atom_ids:      Subset of retrieved_atom_ids that were actually cited or
                            influenced the response.  Pass [] if memory was retrieved
                            but not used, or same as retrieved_atom_ids if all were used.
        context_status:     Quality of the retrieved context.  One of:
                            'sufficient' (default), 'insufficient', 'stale',
                            'conflicting', 'unsupported', 'needs_verification',
                            'needs_user_clarification', 'unsafe_or_blocked'.
        verdict:            Response quality verdict.  One of:
                            'approved' (default), 'needs_caveat', 'needs_revision',
                            'needs_verification', 'blocked'.
        confidence:         Confidence in the response, 0.0–1.0.  Default 0.85.
        reasoning:          Short note on how memory influenced the response.
        source:             Tag identifying the caller.  Default 'mcp_copilot'.

    Returns:
        dict with 'context_trace_id' and 'response_trace_id'.
    """
    retrieved = list(retrieved_atom_ids or [])
    used = list(used_atom_ids or [])

    # Sanitise CHECK-constrained fields
    if context_status not in _VALID_CONTEXT_STATUSES:
        context_status = "sufficient"
    if verdict not in _VALID_VERDICTS:
        verdict = "approved"
    confidence = max(0.0, min(1.0, float(confidence)))

    # Determine final_action from context_status
    _action_map = {
        "sufficient": "answer",
        "needs_verification": "verify_then_answer",
        "conflicting": "answer_with_caveat",
        "stale": "answer_with_caveat",
        "needs_user_clarification": "ask_user",
        "unsafe_or_blocked": "refuse",
    }
    final_action = _action_map.get(context_status, "answer")

    task_summary = (user_message[:200] + "…") if len(user_message) > 200 else user_message

    cfg = get_config()

    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            # 1. Write context trace
            cur.execute(
                """
                INSERT INTO runtime_context_traces (
                    task_summary, retrieved_atom_ids, used_atom_ids,
                    ignored_atom_ids, context_status, confidence,
                    issues, required_actions, final_action
                )
                VALUES (
                    %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s,
                    %s::jsonb, %s::jsonb, %s
                )
                RETURNING id;
                """,
                (
                    task_summary,
                    json.dumps(retrieved),
                    json.dumps(used),
                    json.dumps([]),      # ignored_atom_ids — not tracked at MCP level
                    context_status,
                    confidence,
                    json.dumps([]),      # issues
                    json.dumps([]),      # required_actions
                    final_action,
                ),
            )
            context_trace_id = str(cur.fetchone()[0])

            # 2. Write response trace linked to the context trace
            cur.execute(
                """
                INSERT INTO runtime_response_traces (
                    user_message, draft_answer, final_answer,
                    verdict, overstatement_risk,
                    issues, commit_candidates, reasoning,
                    context_trace_id,
                    gap_status, gap_searches, gap_clarifying_question
                )
                VALUES (
                    %s, %s, %s,
                    %s, %s,
                    %s::jsonb, %s::jsonb, %s,
                    %s::uuid,
                    %s, %s, %s
                )
                RETURNING id;
                """,
                (
                    (user_message or "")[:1000],
                    (assistant_response or "")[:4000],
                    (assistant_response or "")[:4000],
                    verdict,
                    "none",                  # overstatement_risk
                    json.dumps([]),           # issues
                    json.dumps([]),           # commit_candidates
                    (f"[{source}] " + (reasoning or ""))[:500],
                    context_trace_id,
                    "resolved",              # gap_status
                    0,                       # gap_searches
                    None,                    # gap_clarifying_question
                ),
            )
            response_trace_id = str(cur.fetchone()[0])

        conn.commit()

    _logger.info(
        "log_turn: wrote response_trace=%s context_trace=%s source=%s",
        response_trace_id, context_trace_id, source,
    )
    return {
        "response_trace_id": response_trace_id,
        "context_trace_id": context_trace_id,
        "status": "logged",
    }
