from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import psycopg

from app.config import get_config
from app.memory_store import MemoryStore


def store_memory_approved(
    proposal_id: str,
    approval_token: str,
) -> dict[str, Any]:
    """Store a confirmation-path candidate after CLI review and approval.

    Looks up the proposal by ID, validates the CLI-issued approval token
    (must match, must not be expired, status must be 'approved'), then
    writes the memory_atom + memory_signal and marks the proposal as 'used'.

    The approval token is issued by `make review-proposals` (scripts/review_proposals.py).
    It is single-use and expires 5 minutes after issuance. Copilot cannot
    generate valid tokens — human review via the CLI is mandatory.

    Args:
        proposal_id: UUID of the proposal returned by memory_propose_signal.
        approval_token: Short-lived token issued by the CLI review step.

    Returns:
        Canonical write report on success:
            {
                "stored": True,
                "memory_atom_id": "<uuid>",
                "memory_signal_id": "<uuid>",
                "content": "<str>",
                "memory_type": "<str>",
                "scope": "<str|null>",
                "relationship": "<str>",
                "signal_created": True,
                "auto_stored": False
            }
        or {"stored": False, "error": "<message>"} on failure.
    """
    if not proposal_id or not proposal_id.strip():
        return {"stored": False, "error": "proposal_id must not be empty"}

    if not approval_token or not approval_token.strip():
        return {"stored": False, "error": "approval_token must not be empty"}

    config = get_config()

    try:
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, status, content, context_summary, memory_type,
                           scope, confidence, importance,
                           relationship, reconciliation_reason, matched_memory_ids,
                           approval_token, token_expires_at
                    FROM memory_proposals
                    WHERE id = %s
                    """,
                    (proposal_id.strip(),),
                )
                row = cur.fetchone()

            if row is None:
                return {"stored": False, "error": f"proposal '{proposal_id}' not found"}

            (
                p_id, p_status, p_content, p_context_summary, p_memory_type,
                p_scope, p_confidence, p_importance,
                p_relationship, p_reconciliation_reason, p_matched_ids_json,
                p_token, p_token_expires_at,
            ) = row

            if p_status == "used":
                return {"stored": False, "error": "proposal already used"}

            if p_status == "rejected":
                return {"stored": False, "error": "proposal was rejected"}

            if p_status == "expired":
                return {"stored": False, "error": "proposal has expired"}

            if p_status != "approved":
                return {
                    "stored": False,
                    "error": (
                        f"proposal status is '{p_status}', expected 'approved'. "
                        "Run `make review-proposals` to approve it first."
                    ),
                }

            if p_token is None or p_token != approval_token.strip():
                return {"stored": False, "error": "invalid approval token"}

            now_utc = datetime.now(tz=timezone.utc)
            if p_token_expires_at is None or now_utc > p_token_expires_at:
                # Mark expired so future calls get a clear message
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE memory_proposals SET status = 'expired' WHERE id = %s",
                        (p_id,),
                    )
                conn.commit()
                return {
                    "stored": False,
                    "error": "approval token has expired. Run `make review-proposals` again.",
                }

            # Token is valid — mark as used before writing (prevents double-store)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE memory_proposals
                    SET status = 'used', reviewed_at = now()
                    WHERE id = %s
                    """,
                    (p_id,),
                )
            conn.commit()

        # Write memory outside the proposal transaction so a store failure
        # doesn't silently consume the token.
        matched_ids: list[str] | None = None
        if p_matched_ids_json:
            try:
                matched_ids = json.loads(p_matched_ids_json)
            except (ValueError, TypeError):
                matched_ids = None

        signal_metadata: dict | None = None
        if matched_ids:
            signal_metadata = {"matched_memory_ids": matched_ids}

        store = MemoryStore()
        atom_id, signal_id = store.store_memory_with_signal(
            content=p_content,
            context_summary=p_context_summary or p_content,
            memory_type=p_memory_type,
            scope=p_scope,
            confidence=float(p_confidence),
            importance=float(p_importance),
            relationship=p_relationship,
            reconciliation_reason=p_reconciliation_reason,
            signal_metadata=signal_metadata,
        )

        return {
            "stored": True,
            "memory_atom_id": atom_id,
            "memory_signal_id": signal_id,
            "content": p_content,
            "memory_type": p_memory_type,
            "scope": p_scope,
            "relationship": p_relationship,
            "signal_created": True,
            "auto_stored": False,
        }

    except Exception as exc:
        return {"stored": False, "error": f"store_memory_approved failed: {exc}"}
