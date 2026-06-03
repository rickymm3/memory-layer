"""MCP tool: ingest a conversation transcript from any LLM.

Thin wrapper around app/ingest.ingest_transcript.
"""
from __future__ import annotations

from typing import Any

from app.ingest import ingest_transcript as _ingest


def ingest_conversation_transcript(
    turns: list[dict[str, str]],
    source_label: str = "external",
    scope: str | None = None,
) -> dict[str, Any]:
    """Import a conversation from any LLM and commit durable memories.

    Pass a list of chat turns exported from Claude, GPT-4, Gemini, or any tool.
    The extraction LLM judges what is worth storing; each candidate passes
    through the full commit pipeline (reconcile → critic → risk gate) before
    any write.

    Args:
        turns: List of {"role": "user"|"assistant", "content": str} dicts in
               chronological order.
        source_label: Name of the source LLM or tool stored in memory_signals
                      for provenance. Examples: "claude-3-7-sonnet", "gpt-4o",
                      "gemini-pro", "local_user". Default "external".
        scope: Optional scope override. Pass None to let the extractor assign
               scope per-candidate (recommended — cross-project user preferences
               will naturally land in the "user" scope).

    Returns:
        {
            "committed":       list of stored atoms,
            "proposed":        list of candidates queued for human review,
            "skipped":         int,
            "turns_processed": int,
            "errors":          list of non-fatal per-turn error messages
        }
    """
    if not isinstance(turns, list) or not turns:
        return {
            "committed": [], "proposed": [], "skipped": 0,
            "turns_processed": 0,
            "errors": ["turns must be a non-empty list of {role, content} dicts."],
        }

    return _ingest(turns=turns, source_label=source_label, scope=scope)
