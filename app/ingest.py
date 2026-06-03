"""Transcript ingest — import conversations from any LLM into the memory store.

Accepts a list of chat turns ({"role": "user"|"assistant", "content": str})
and runs the full commit pipeline on each user+assistant pair.  This is the
entry point for importing memory from external conversations — Claude, GPT-4,
Gemini, or any tool that can export a chat log.

Source tracking
---------------
Pass ``source_label`` to identify where the conversation came from.  It is
stored on every ``memory_signal`` written during the ingest so you can later
filter, weight, or audit by origin.

Examples:
    "github_copilot", "gpt-4o", "claude-3-7-sonnet", "gemini-pro", "local_user"

Scope
-----
Pass ``scope`` to force all extracted atoms into a specific scope.
Leave as None to let the extraction LLM decide per-candidate (recommended for
cross-project user-level preferences — they will naturally land in ``user`` scope).
"""
from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def ingest_transcript(
    turns: list[dict[str, str]],
    source_label: str = "external",
    scope: str | None = None,
) -> dict[str, Any]:
    """Process a conversation transcript and commit durable memories.

    Args:
        turns: List of {"role": "user"|"assistant", "content": str} dicts in
               chronological order. At least one "user" turn is required.
        source_label: Human-readable name of the originating LLM or tool.
                      Stored in memory_signals.source_key for provenance.
        scope: Optional scope override for all extracted atoms. Pass None to
               let the extraction LLM assign scope per-candidate.

    Returns:
        {
            "committed":     [{"atom_id", "final_memory_text", "memory_type",
                               "scope", "write_action"}, ...],
            "proposed":      [{"candidate_text", "proposal_id", "reason"}, ...],
            "skipped":       int,
            "turns_processed": int,
            "errors":        [str, ...]  # non-fatal per-turn failures
        }
    """
    from scripts.extract_memory_candidates import extract_candidates_from_turn
    from app.commit_pipeline import MemoryCommitPipeline

    if not turns:
        return {
            "committed": [], "proposed": [], "skipped": 0,
            "turns_processed": 0, "errors": ["No turns provided."],
        }

    committed: list[dict] = []
    proposed: list[dict] = []
    skipped = 0
    errors: list[str] = []
    turns_processed = 0

    pipeline = MemoryCommitPipeline()

    # Walk turns in pairs: find each user turn and pair it with the following
    # assistant turn (if any).  Unpaired user turns are still processed.
    i = 0
    while i < len(turns):
        turn = turns[i]
        role = str(turn.get("role", "")).lower()
        if role != "user":
            i += 1
            continue

        user_msg = str(turn.get("content", "")).strip()
        if not user_msg:
            i += 1
            continue

        # Look for the immediately following assistant turn
        assistant_content = ""
        if i + 1 < len(turns) and str(turns[i + 1].get("role", "")).lower() == "assistant":
            assistant_content = str(turns[i + 1].get("content", "")).strip()
            i += 2
        else:
            i += 1

        turns_processed += 1

        try:
            extracted = extract_candidates_from_turn(
                user_msg=user_msg,
                thinking="",
                answer=assistant_content,
                include_rejected=False,
            )
        except Exception as exc:
            errors.append(f"Turn {turns_processed}: extraction failed — {exc}")
            continue

        candidates = [
            c for c in extracted.get("candidates", [])
            if isinstance(c, dict) and c.get("should_store")
        ]

        if not candidates:
            continue

        for candidate in candidates:
            # Apply scope override if provided
            if scope:
                candidate["scope"] = scope
            # Tag the source for signal provenance
            candidate["source_key"] = source_label
            candidate["source_type"] = "external_ingest"

            try:
                decision = pipeline.commit_candidate(
                    candidate,
                    source_key=source_label,
                    source_type="external_ingest",
                )
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
                _logger.warning("ingest_transcript: pipeline failed for candidate: %s", exc)
                skipped += 1

    return {
        "committed": committed,
        "proposed": proposed,
        "skipped": skipped,
        "turns_processed": turns_processed,
        "errors": errors,
    }
