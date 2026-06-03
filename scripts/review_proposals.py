#!/usr/bin/env python3
"""Review pending memory proposals and issue approval tokens.

Usage:
    make review-proposals
    # or directly:
    .venv/bin/python scripts/review_proposals.py [--scope SCOPE] [--limit N]

For each pending proposal the script shows the candidate and prompts for:
    [a]pprove  — generate a single-use 5-minute token, print it.
    [r]eject   — mark rejected.
    [s]kip     — leave as pending_review, review later.

After approving, copy the printed token and tell Copilot:
    Call memory_store_approved with proposal_id=<id> and approval_token=<token>
"""
from __future__ import annotations

import argparse
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from textwrap import fill

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from app.config import get_config

_TOKEN_TTL_SECONDS = 300  # 5 minutes
_DIVIDER = "─" * 72


def _fmt_ts(ts: datetime | None) -> str:
    if ts is None:
        return "—"
    return ts.strftime("%Y-%m-%d %H:%M UTC")


def _print_proposal(idx: int, total: int, row: tuple) -> None:
    (
        p_id, p_status, p_content, p_context_summary, p_memory_type,
        p_scope, p_confidence, p_importance,
        p_relationship, p_reconciliation_reason, p_matched_ids_json,
        p_created_at,
    ) = row

    print(_DIVIDER)
    print(f"Proposal {idx}/{total}  •  ID: {p_id}")
    print(f"Created : {_fmt_ts(p_created_at)}")
    print(f"Type    : {p_memory_type}  |  Scope: {p_scope or '—'}  |  Relationship: {p_relationship}")
    print(f"Confidence: {p_confidence:.2f}  |  Importance: {p_importance:.2f}")
    print()
    print("Content:")
    print(fill(p_content, width=72, initial_indent="  ", subsequent_indent="  "))
    if p_context_summary and p_context_summary != p_content:
        print()
        print("Summary:")
        print(fill(p_context_summary, width=72, initial_indent="  ", subsequent_indent="  "))
    if p_reconciliation_reason:
        print()
        print("Reconciler reason:")
        print(fill(p_reconciliation_reason, width=72, initial_indent="  ", subsequent_indent="  "))
    if p_matched_ids_json:
        print()
        print(f"Matched atom IDs: {p_matched_ids_json}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Review pending memory proposals and issue approval tokens."
    )
    parser.add_argument("--scope", default=None, help="Filter proposals by scope")
    parser.add_argument("--limit", type=int, default=20, help="Max proposals to review (default 20)")
    parser.add_argument(
        "--yes-all",
        action="store_true",
        help="Approve all pending proposals without prompting (use with care)",
    )
    args = parser.parse_args()

    config = get_config()

    scope_clause = "AND scope = %s" if args.scope else ""
    params: list = []
    if args.scope:
        params.append(args.scope)
    params.append(max(1, args.limit))

    try:
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, status, content, context_summary, memory_type,
                           scope, confidence, importance,
                           relationship, reconciliation_reason, matched_memory_ids,
                           created_at
                    FROM memory_proposals
                    WHERE status = 'pending_review'
                    {scope_clause}
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    params,
                )
                rows = cur.fetchall()
    except Exception as exc:
        print(f"[ERROR] DB connection failed: {exc}", file=sys.stderr)
        return 1

    if not rows:
        print("No pending proposals.")
        return 0

    total = len(rows)
    print(f"\n{total} pending proposal(s).\n")

    approved_count = 0
    rejected_count = 0
    skipped_count = 0

    for idx, row in enumerate(rows, start=1):
        _print_proposal(idx, total, row)
        p_id = row[0]

        if args.yes_all:
            choice = "a"
        else:
            try:
                choice = input("[a]pprove / [r]eject / [s]kip > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\nAborted.")
                break

        if choice.startswith("a"):
            token = secrets.token_hex(16)
            expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=_TOKEN_TTL_SECONDS)
            try:
                with psycopg.connect(config.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE memory_proposals
                            SET status = 'approved',
                                approval_token = %s,
                                token_expires_at = %s,
                                reviewed_at = now()
                            WHERE id = %s
                            """,
                            (token, expires_at, p_id),
                        )
                    conn.commit()
            except Exception as exc:
                print(f"[ERROR] Failed to approve proposal {p_id}: {exc}", file=sys.stderr)
                continue

            print(_DIVIDER)
            print("APPROVED — token issued (valid 5 minutes, single-use):")
            print()
            print(f"  Proposal ID   : {p_id}")
            print(f"  Approval token: {token}")
            print(f"  Expires at    : {_fmt_ts(expires_at)}")
            print()
            print("Tell Copilot:")
            print(f'  memory_store_approved(proposal_id="{p_id}", approval_token="{token}")')
            print(_DIVIDER)
            approved_count += 1

        elif choice.startswith("r"):
            try:
                with psycopg.connect(config.database_url) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE memory_proposals
                            SET status = 'rejected', reviewed_at = now()
                            WHERE id = %s
                            """,
                            (p_id,),
                        )
                    conn.commit()
            except Exception as exc:
                print(f"[ERROR] Failed to reject proposal {p_id}: {exc}", file=sys.stderr)
                continue

            print("Rejected.")
            rejected_count += 1

        else:
            print("Skipped.")
            skipped_count += 1

    print()
    print(
        f"Done. Approved: {approved_count}  |  Rejected: {rejected_count}  |  Skipped: {skipped_count}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
