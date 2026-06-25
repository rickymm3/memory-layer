"""MCP tool: memory_push_conversation

Processes a conversation transcript (text or JSONL path) into memory atoms.
Post drafts are generated automatically by the commit pipeline after atoms land —
they appear in /drafts once the confidence×importance threshold is crossed.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def push_conversation_tool(
    transcript: str,
    source_user_id: str | None = None,
    is_jsonl_path: bool = False,
) -> dict[str, Any]:
    """Commit all turns in a conversation transcript to memory atoms.

    Args:
        transcript: Either a raw conversation transcript (alternating
            User:/Assistant: lines), or the path to a session .jsonl file
            if is_jsonl_path=True.
        source_user_id: Username to tag committed atoms with.
        is_jsonl_path: When True, treat transcript as a filesystem path
            to a Claude Code session .jsonl file.

    Returns:
        Summary dict with committed_atoms, proposed_atoms, skipped_turns counts.
        Post drafts surface in /drafts automatically — check there after running.
    """
    if is_jsonl_path:
        jsonl_path = transcript.strip()
    else:
        # Write the raw transcript to a temp file in pseudo-JSONL format
        # so push_conversation can parse it uniformly.
        jsonl_path = _transcript_to_jsonl(transcript)

    try:
        from scripts.push_conversation import push_conversation
        result = push_conversation(
            jsonl_path=jsonl_path,
            username=source_user_id,
            dry_run=False,
            verbose=False,
        )
    finally:
        # Clean up temp file if we created one
        if not is_jsonl_path and os.path.exists(jsonl_path):
            try:
                os.unlink(jsonl_path)
            except Exception:
                pass

    if "error" in result:
        return {"error": result["error"]}

    committed = result.get("committed_atoms", 0)
    return {
        "committed_atoms": committed,
        "proposed_atoms": result.get("proposed_atoms", 0),
        "skipped_turns": result.get("skipped_turns", 0),
        "total_turns": result.get("total_turns", 0),
        "atom_ids": result.get("atom_ids", [])[:20],  # cap to avoid huge payloads
        "message": (
            f"Committed {committed} atom(s) from this conversation. "
            "Post drafts are generated automatically — check /drafts for suggestions."
        ),
    }


def _transcript_to_jsonl(transcript: str) -> str:
    """Convert a plain-text transcript to a minimal JSONL file.

    Recognises lines starting with 'User:', 'Human:', 'Assistant:', or 'AI:'
    as role markers. Falls back to alternating user/assistant if no markers found.
    """
    lines = transcript.strip().splitlines()
    messages: list[dict] = []
    current_role: str | None = None
    current_parts: list[str] = []

    def _flush():
        if current_role and current_parts:
            messages.append({"role": current_role, "content": " ".join(current_parts).strip()})

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith(("user:", "human:")):
            _flush()
            current_parts = [stripped.split(":", 1)[1].strip()]
            current_role = "user"
        elif lower.startswith(("assistant:", "ai:")):
            _flush()
            current_parts = [stripped.split(":", 1)[1].strip()]
            current_role = "assistant"
        else:
            if current_role:
                current_parts.append(stripped)

    _flush()

    if not messages:
        # No role markers — treat as alternating user/assistant blocks
        blocks = [b.strip() for b in transcript.split("\n\n") if b.strip()]
        for i, block in enumerate(blocks):
            messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": block})

    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, prefix="push_convo_"
    )
    for msg in messages:
        tf.write(json.dumps({"message": msg}) + "\n")
    tf.close()
    return tf.name
