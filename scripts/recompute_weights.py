#!/usr/bin/env python3
"""CLI: recompute signal-aggregation weights for one or all memory atoms.

Fetches each atom's linked signals, runs the aggregator, and writes
support_weight, opposition_weight, disagreement_score, confidence, and
last_recomputed_at back to memory_atoms.

Usage:
    .venv/bin/python scripts/recompute_weights.py           # all atoms
    .venv/bin/python scripts/recompute_weights.py <atom_id> # single atom
    make recompute-weights
    make recompute-weights ARGS=<atom_id>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.memory_store import MemoryStore


def main() -> int:
    args = sys.argv[1:]
    store = MemoryStore()

    if args:
        atom_id = args[0].strip()
        print(f"Recomputing atom {atom_id} ...")
        result = store.recompute_atom_weights(atom_id)
        if result is None:
            print(f"ERROR: atom {atom_id} not found.")
            return 1
        print(
            f"  confidence={result['confidence']:.3f}"
            f"  support={result['support_weight']:.4f}"
            f"  opposition={result['opposition_weight']:.4f}"
            f"  disagreement={result['disagreement_score']:.4f}"
        )
        print("Done.")
        return 0

    # Recompute all atoms
    config = get_config()
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM memory_atoms ORDER BY created_at;")
            atom_ids = [str(row[0]) for row in cur.fetchall()]

    if not atom_ids:
        print("No atoms found.")
        return 0

    print(f"Recomputing {len(atom_ids)} atom(s) ...")
    for atom_id in atom_ids:
        result = store.recompute_atom_weights(atom_id)
        if result:
            print(
                f"  {atom_id[:8]}..."
                f"  confidence={result['confidence']:.3f}"
                f"  support={result['support_weight']:.4f}"
                f"  opposition={result['opposition_weight']:.4f}"
                f"  disagreement={result['disagreement_score']:.4f}"
            )
        else:
            print(f"  {atom_id[:8]}...  NOT FOUND (skipped)")

    print(f"Done. Recomputed {len(atom_ids)} atom(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
