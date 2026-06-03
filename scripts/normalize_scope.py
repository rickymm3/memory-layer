#!/usr/bin/env python3
"""
Bulk-rename a legacy scope string to a canonical scope string in memory_atoms.

Does NOT modify memory_signals — signal scope values are immutable provenance
records and must remain historically accurate.

Does NOT regenerate embeddings — scope is metadata only.

Usage:
    python scripts/normalize_scope.py --from memory-layer-project --to project:memory-layer
    python scripts/normalize_scope.py --from memory-layer-project --to project:memory-layer --yes
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config


def _confirm(prompt: str) -> bool:
    answer = input(prompt).strip().lower()
    return answer in {"y", "yes"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rename a legacy scope to a canonical scope in memory_atoms."
    )
    parser.add_argument("--from", dest="from_scope", required=True, help="Legacy scope string to rename FROM.")
    parser.add_argument("--to", dest="to_scope", required=True, help="Canonical scope string to rename TO.")
    parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Skip interactive confirmation and apply immediately.",
    )
    args = parser.parse_args()

    if args.from_scope == args.to_scope:
        print("[SKIP] --from and --to are identical. Nothing to do.")
        return 0

    cfg = get_config()

    with psycopg.connect(cfg.database_url) as conn:
        with conn.cursor() as cur:
            # --- Before counts ---
            cur.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE scope = %s",
                (args.from_scope,),
            )
            from_count_before: int = cur.fetchone()[0]  # type: ignore[index]

            cur.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE scope = %s",
                (args.to_scope,),
            )
            to_count_before: int = cur.fetchone()[0]  # type: ignore[index]

            if from_count_before == 0:
                print(f"[INFO] No atoms found with scope={args.from_scope!r}. Nothing to migrate.")
                return 0

            # --- List candidates ---
            cur.execute(
                """
                SELECT id, memory_type, lifecycle_status, content
                FROM memory_atoms
                WHERE scope = %s
                ORDER BY created_at
                """,
                (args.from_scope,),
            )
            rows = cur.fetchall()

            print(f"\nAtoms to migrate from {args.from_scope!r} → {args.to_scope!r}:")
            print(f"  {'ID':36s}  {'TYPE':12s}  {'STATUS':10s}  CONTENT")
            print(f"  {'-'*36}  {'-'*12}  {'-'*10}  {'-'*60}")
            ambiguous = []
            for row in rows:
                atom_id, memory_type, lifecycle_status, content = row
                preview = str(content)[:70]
                print(f"  {str(atom_id):36s}  {str(memory_type):12s}  {str(lifecycle_status):10s}  {preview}")
                # Flag any archived atoms as ambiguous — they are hidden from retrieval
                # and may have been intentionally isolated. Report; do not skip automatically.
                if lifecycle_status == "archived":
                    ambiguous.append(atom_id)

            print(f"\nBefore:")
            print(f"  scope={args.from_scope!r:40s}  count={from_count_before}")
            print(f"  scope={args.to_scope!r:40s}  count={to_count_before}")
            print(f"\nAfter (projected):")
            print(f"  scope={args.from_scope!r:40s}  count=0")
            print(f"  scope={args.to_scope!r:40s}  count={to_count_before + from_count_before}")

            if ambiguous:
                print(f"\n[WARN] {len(ambiguous)} atom(s) have lifecycle_status='archived' and will also be migrated.")
                print("       Archived atoms are hidden from standard retrieval regardless of scope.")
                print("       IDs:", ", ".join(str(a) for a in ambiguous))

            print(f"\nSignals: memory_signals.scope is historical provenance — NOT updated.")
            print(f"Embeddings: scope-only change — NOT regenerated.\n")

            if not args.yes:
                if not _confirm(f"Apply migration? Moves {from_count_before} atom(s). [y/N]: "):
                    print("[CANCELLED] No changes made.")
                    return 0

            # --- Perform migration ---
            cur.execute(
                "UPDATE memory_atoms SET scope = %s WHERE scope = %s",
                (args.to_scope, args.from_scope),
            )
            moved = cur.rowcount
            conn.commit()

            # --- After counts ---
            cur.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE scope = %s",
                (args.from_scope,),
            )
            from_count_after: int = cur.fetchone()[0]  # type: ignore[index]

            cur.execute(
                "SELECT COUNT(*) FROM memory_atoms WHERE scope = %s",
                (args.to_scope,),
            )
            to_count_after: int = cur.fetchone()[0]  # type: ignore[index]

            print(f"[OK] Moved {moved} atom(s).")
            print(f"\nAfter:")
            print(f"  scope={args.from_scope!r:40s}  count={from_count_after}")
            print(f"  scope={args.to_scope!r:40s}  count={to_count_after}")
            if ambiguous:
                print(f"\n[INFO] {len(ambiguous)} archived atom(s) were included in the migration.")
                print("       They remain archived and are not surfaced in standard retrieval.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
