#!/usr/bin/env python3
"""Write a single model observation to the memory layer.

This is the fast path for capturing model behavioral lessons without
running the full reflect pipeline (which requires ANTHROPIC_API_KEY).

The observation must have both:
  --content   Full description of what was observed (the atom content)
  --injection Actionable directive to prepend to future prompts (context_summary)

Usage:
    python scripts/observe_model.py \\
        --model claude-sonnet-4-6 \\
        --content "Claude adds unrequested abstractions when fixing targeted bugs." \\
        --injection "Specify 'change only the identified function, no helpers or refactoring'."

    python scripts/observe_model.py \\
        --model qwen3-8b \\
        --content "qwen3-8b returns markdown lists when asked for strict JSON arrays." \\
        --injection "Do not use qwen3-8b for structured JSON extraction tasks." \\
        --importance 0.9

The write creates one memory_atom + one linked memory_signal (dual-write).
IDs are printed on success.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from mcp_server.tools.store_auto import store_memory_auto


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Write a model behavioral observation to the memory layer."
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model ID without 'model:' prefix (e.g. claude-sonnet-4-6)",
    )
    parser.add_argument(
        "--content",
        required=True,
        help="Full description of the observed behavior (the atom content)",
    )
    parser.add_argument(
        "--injection",
        required=True,
        help=(
            "Actionable directive for future prompts (the context_summary). "
            "Must be a verb-first imperative, e.g. 'Require explicit scope constraint: only change files mentioned in the task.'"
        ),
    )
    parser.add_argument(
        "--importance",
        type=float,
        default=0.8,
        help="Importance weight 0.0–1.0 (default: 0.8)",
    )
    args = parser.parse_args()

    scope = f"model:{args.model}"
    importance = max(0.0, min(1.0, args.importance))

    # Validate injection string looks like an imperative
    injection = args.injection.strip()
    if not injection:
        print("[ERROR] --injection cannot be empty", file=sys.stderr)
        return 1

    print(f"Storing model observation for scope={scope!r}…")
    print(f"  Content  : {args.content[:80]}{'…' if len(args.content) > 80 else ''}")
    print(f"  Injection: {injection[:80]}{'…' if len(injection) > 80 else ''}")
    print(f"  Importance: {importance}")
    print()

    result = store_memory_auto(
        content=args.content,
        memory_type="observation",
        scope=scope,
        importance=importance,
        relationship="new",
        context_summary=injection,
    )

    action = result.get("write_action") or result.get("decision", "unknown")
    atom_id = result.get("memory_atom_id")
    signal_id = result.get("memory_signal_id")

    if action == "commit":
        print(f"[OK] Stored: action={action}")
        print(f"     memory_atom_id  : {atom_id}")
        print(f"     memory_signal_id: {signal_id}")
        return 0
    if action in ("reinforce_existing", "refine_existing"):
        print(f"[OK] {action}: matched an existing atom — signal added to reinforce it")
        if atom_id:
            print(f"     memory_atom_id  : {atom_id}")
        if signal_id:
            print(f"     memory_signal_id: {signal_id}")
        return 0
    elif action == "reject":
        reason = result.get("rejection_reason", "(no reason)")
        print(f"[REJECTED] {reason}", file=sys.stderr)
        notes = result.get("critic_notes", [])
        for n in notes:
            print(f"  critic: {n}", file=sys.stderr)
        return 1
    else:
        print(f"[{action.upper()}] atom_id={atom_id}  signal_id={signal_id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
