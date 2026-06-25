#!/usr/bin/env python3
"""Automatic per-turn memory extraction — called by the Stop hook.

Reads the last user+assistant turn from a Claude Code session JSONL file
and runs it through run_turn_reflection(). Commits any extracted atoms
without requiring model cooperation.

Usage (from Stop hook):
    python scripts/auto_extract_turn.py <jsonl_path> [--user <username>]

Exit codes:
    0 — success (including "nothing to extract")
    1 — error reading file or running extraction (non-fatal from hook perspective)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.WARNING)


def _read_last_turn(jsonl_path: str) -> tuple[str, str]:
    """Return (last_user_message, last_assistant_message) from the session JSONL.

    Scans backwards to find the most recent complete user→assistant pair.
    Returns ('', '') if no complete pair is found.
    """
    path = Path(jsonl_path)
    if not path.exists():
        return "", ""

    lines = path.read_bytes().splitlines()
    last_user = ""
    last_assistant = ""

    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except Exception:
            continue

        msg = d.get("message", {})
        role = msg.get("role")
        content = msg.get("content", "")

        # Flatten content blocks
        if isinstance(content, list):
            text = " ".join(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = str(content)

        text = text.strip()
        if not text:
            continue

        if role == "assistant" and not last_assistant:
            last_assistant = text
        elif role == "user" and not last_user and last_assistant:
            last_user = text
            break  # complete pair found

    return last_user, last_assistant


def _is_trivial(user_msg: str) -> bool:
    """Skip extraction for very short or small-talk turns."""
    stripped = user_msg.strip()
    if len(stripped) < 15:
        return True
    small_talk = {"hi", "hello", "hey", "thanks", "thank you", "ok", "okay",
                  "yes", "no", "sure", "great", "cool", "nice", "good", "bye",
                  "goodbye", "lol", "yep", "nope"}
    words = stripped.lower().split()
    content_words = [w for w in words if w not in small_talk]
    return len(content_words) < 3


def main() -> int:
    p = argparse.ArgumentParser(description="Auto-extract memory from last session turn")
    p.add_argument("jsonl_path", help="Path to the session .jsonl file")
    p.add_argument("--user", default=None, help="source_user_id to tag atoms with")
    args = p.parse_args()

    try:
        from dotenv import load_dotenv
        from pathlib import Path as _Path
        load_dotenv(_Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass

    user_msg, assistant_msg = _read_last_turn(args.jsonl_path)

    if not user_msg or not assistant_msg:
        return 0  # no complete turn — nothing to do

    if _is_trivial(user_msg):
        return 0  # small-talk turn — skip extraction

    try:
        from app.reflection import run_turn_reflection
        result = run_turn_reflection(
            user_msg=user_msg,
            thinking="",
            answer=assistant_msg,
            source_user_id=args.user,
        )
        committed = result.get("committed", [])
        if committed:
            for atom in committed:
                print(
                    f"[auto-extract] atom_id={atom.get('atom_id')} "
                    f"type={atom.get('memory_type')} scope={atom.get('scope')}",
                    file=sys.stderr,
                )
    except Exception as exc:
        print(f"[auto-extract] extraction failed (non-fatal): {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
