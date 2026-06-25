#!/usr/bin/env python3
"""Push an entire conversation session into memory atoms and queue post drafts.

Reads a Claude Code session JSONL file, iterates over all user→assistant turns,
commits durable memory atoms for each substantive turn, and queues Synapse post
drafts for any atoms that cross the confidence×importance threshold.

Usage:
    python scripts/push_conversation.py <jsonl_path> [--user <username>] [--dry-run]
    make push-convo ARGS="<jsonl_path> [--user <username>] [--dry-run]"

Options:
    --user      Username to tag atoms with (maps to source_user_id).
    --dry-run   Extract and print candidates without writing anything.
    --verbose   Print each turn's extraction result.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)
_logger = logging.getLogger(__name__)

_MIN_TURN_CHARS = 20
_SMALL_TALK = {
    "hi", "hello", "hey", "thanks", "thank you", "ok", "okay", "yes", "no",
    "sure", "great", "cool", "nice", "good", "bye", "goodbye", "lol",
    "yep", "nope", "got it", "sounds good",
}


def _read_all_turns(jsonl_path: str) -> list[tuple[str, str]]:
    """Return list of (user_msg, assistant_msg) pairs from a session JSONL.

    Processes the file in chronological order. Each complete user→assistant
    exchange is returned as a tuple. Tool-use messages are skipped.
    """
    path = Path(jsonl_path)
    if not path.exists():
        print(f"[push-convo] File not found: {jsonl_path}", file=sys.stderr)
        return []

    lines = path.read_bytes().splitlines()

    turns: list[tuple[str, str]] = []
    pending_user: str | None = None

    for raw in lines:
        try:
            d = json.loads(raw)
        except Exception:
            continue

        msg = d.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")

        if isinstance(content, list):
            text_parts = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    text_parts.append(b.get("text", ""))
            text = " ".join(text_parts).strip()
        else:
            text = str(content).strip()

        if not text:
            continue

        if role == "user":
            pending_user = text
        elif role == "assistant" and pending_user:
            turns.append((pending_user, text))
            pending_user = None

    return turns


def _is_trivial(user_msg: str) -> bool:
    stripped = user_msg.strip()
    if len(stripped) < _MIN_TURN_CHARS:
        return True
    words = stripped.lower().split()
    content_words = [w for w in words if w not in _SMALL_TALK]
    return len(content_words) < 3


def push_conversation(
    jsonl_path: str,
    username: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> dict:
    """Process all turns in a session JSONL and commit atoms + queue post drafts.

    Returns a summary dict with total_turns, processed_turns, committed_atoms,
    proposed_atoms, skipped_turns, and post_drafts_queued counts.
    """
    turns = _read_all_turns(jsonl_path)
    if not turns:
        return {
            "total_turns": 0, "processed_turns": 0,
            "committed_atoms": 0, "proposed_atoms": 0,
            "skipped_turns": 0, "post_drafts_queued": 0,
        }

    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    total = len(turns)
    processed = 0
    committed_atoms: list[dict] = []
    proposed_count = 0
    skipped = 0
    drafts_queued = 0

    if not dry_run:
        try:
            from app.reflection import run_turn_reflection
        except Exception as exc:
            print(f"[push-convo] Failed to import reflection pipeline: {exc}", file=sys.stderr)
            return {"error": str(exc)}

    for i, (user_msg, assistant_msg) in enumerate(turns, 1):
        if _is_trivial(user_msg):
            skipped += 1
            if verbose:
                print(f"[push-convo] Turn {i}/{total}: trivial — skipped", file=sys.stderr)
            continue

        processed += 1

        if dry_run:
            print(f"[push-convo] Turn {i}/{total} (dry-run): {user_msg[:80]!r}…")
            continue

        try:
            result = run_turn_reflection(
                user_msg=user_msg,
                thinking="",
                answer=assistant_msg,
                source_user_id=username,
            )
            turn_committed = result.get("committed", [])
            turn_proposed = result.get("proposed", [])
            committed_atoms.extend(turn_committed)
            proposed_count += len(turn_proposed)

            if verbose:
                print(
                    f"[push-convo] Turn {i}/{total}: "
                    f"committed={len(turn_committed)} proposed={len(turn_proposed)}",
                    file=sys.stderr,
                )
        except Exception as exc:
            _logger.warning("push-convo: turn %d failed: %s", i, exc)
            if verbose:
                print(f"[push-convo] Turn {i}/{total}: error — {exc}", file=sys.stderr)

    # Post draft generation is handled automatically by the commit pipeline's
    # _post_commit_trigger, which fires in a background thread for every atom
    # committed above. Drafts appear in /drafts once the threshold is crossed.

    return {
        "total_turns": total,
        "processed_turns": processed,
        "committed_atoms": len(committed_atoms),
        "proposed_atoms": proposed_count,
        "skipped_turns": skipped,
        "atom_ids": [a["atom_id"] for a in committed_atoms],
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Push an entire conversation session into memory atoms"
    )
    p.add_argument("jsonl_path", help="Path to the session .jsonl file")
    p.add_argument("--user", default=None, help="Username to tag atoms with")
    p.add_argument("--dry-run", action="store_true", help="Print turns without writing")
    p.add_argument("--verbose", action="store_true", help="Print per-turn results")
    args = p.parse_args()

    summary = push_conversation(
        jsonl_path=args.jsonl_path,
        username=args.user,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if "error" in summary:
        print(f"[push-convo] Fatal error: {summary['error']}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(
            f"\n[push-convo] Dry-run complete: "
            f"{summary['processed_turns']}/{summary['total_turns']} turns would be processed "
            f"({summary['skipped_turns']} trivial skipped)"
        )
        return 0

    print(
        f"\n[push-convo] Done.\n"
        f"  Turns processed : {summary['processed_turns']}/{summary['total_turns']} "
        f"({summary['skipped_turns']} trivial skipped)\n"
        f"  Atoms committed : {summary['committed_atoms']}\n"
        f"  Atoms proposed  : {summary['proposed_atoms']} (pending review)\n"
        f"\n  Post drafts are generated automatically in the background.\n"
        f"  Check /drafts — any qualifying atoms will surface as suggested posts.\n"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
