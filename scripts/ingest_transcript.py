#!/usr/bin/env python3
"""CLI: ingest a conversation transcript from any LLM into the memory store.

Usage
-----
  python scripts/ingest_transcript.py --file transcript.json [--source claude-3-7] [--scope user]
  cat transcript.json | python scripts/ingest_transcript.py --source gpt-4o

Transcript format (JSON)
------------------------
A JSON array of turn objects:

    [
        {"role": "user",      "content": "We decided to use Postgres not SQLite."},
        {"role": "assistant", "content": "Got it, Postgres it is."},
        ...
    ]

The "role" field must be "user" or "assistant".
Only user turns are extracted from; assistant turns provide context.

Options
-------
  --file FILE       Path to JSON transcript file. Reads from stdin if omitted.
  --source LABEL    Source label stored in memory_signals.source_key.
                    Examples: claude-3-7-sonnet, gpt-4o, gemini-pro.
                    Default: external
  --scope SCOPE     Force all extracted atoms into this scope.
                    Leave blank to let the extractor assign scope per-candidate.
  --dry-run         Extract candidates without committing anything.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a conversation transcript into the memory store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file", "-f",
        help="Path to JSON transcript file (stdin if omitted).",
    )
    parser.add_argument(
        "--source", "-s",
        default="external",
        help="Source label for memory_signals provenance (e.g. claude-3-7-sonnet).",
    )
    parser.add_argument(
        "--scope",
        default=None,
        help="Scope override for all extracted atoms (e.g. 'user' or 'project:myapp').",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Extract candidates and print them without committing anything.",
    )
    args = parser.parse_args()

    # ── Load transcript ───────────────────────────────────────────────────────
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"Error reading file: {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        raw = sys.stdin.read()

    try:
        turns = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(turns, list):
        print("Transcript must be a JSON array of {role, content} objects.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(turns)} turns from transcript.")

    # ── Dry-run: extract only ─────────────────────────────────────────────────
    if args.dry_run:
        from scripts.extract_memory_candidates import extract_candidates_from_turn

        all_candidates = []
        i = 0
        while i < len(turns):
            turn = turns[i]
            if str(turn.get("role", "")).lower() != "user":
                i += 1
                continue
            user_msg = str(turn.get("content", "")).strip()
            assistant_content = ""
            if i + 1 < len(turns) and str(turns[i + 1].get("role", "")).lower() == "assistant":
                assistant_content = str(turns[i + 1].get("content", "")).strip()
                i += 2
            else:
                i += 1
            if not user_msg:
                continue
            try:
                result = extract_candidates_from_turn(
                    user_msg=user_msg, thinking="", answer=assistant_content,
                    include_rejected=False,
                )
                for c in result.get("candidates", []):
                    all_candidates.append(c)
            except Exception as exc:
                print(f"  [extraction error] {exc}", file=sys.stderr)

        print(f"\nDry run — {len(all_candidates)} candidate(s) extracted (nothing committed):\n")
        for c in all_candidates:
            flag = "STORE" if c.get("should_store") else "skip"
            print(f"  [{flag}] {c.get('memory_type','?')} / {c.get('scope','?')}")
            print(f"         {c.get('content','')}")
            print(f"         reason: {c.get('reason','')}\n")
        return

    # ── Full ingest ───────────────────────────────────────────────────────────
    from app.ingest import ingest_transcript

    print(f"Ingesting with source='{args.source}', scope='{args.scope or 'auto'}'...")
    result = ingest_transcript(
        turns=turns,
        source_label=args.source,
        scope=args.scope,
    )

    print(f"\nResults:")
    print(f"  Turns processed : {result['turns_processed']}")
    print(f"  Committed       : {len(result['committed'])}")
    print(f"  Proposed        : {len(result['proposed'])}")
    print(f"  Skipped         : {result['skipped']}")

    if result["committed"]:
        print("\nCommitted atoms:")
        for a in result["committed"]:
            print(f"  [{a['memory_type']} / {a['scope']}] {a['final_memory_text'][:80]}")

    if result["proposed"]:
        print("\nProposals queued for review:")
        for p in result["proposed"]:
            print(f"  {p['proposal_id']} — {p['candidate_text'][:80]}")

    if result["errors"]:
        print("\nNon-fatal errors:")
        for e in result["errors"]:
            print(f"  {e}")


if __name__ == "__main__":
    main()
