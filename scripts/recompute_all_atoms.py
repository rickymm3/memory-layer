#!/usr/bin/env python3
"""Bulk recompute aggregation weights for all memory atoms.

Fetches every atom id from memory_atoms and calls recompute_atom_weights()
on each. Run this after changing RECENCY_HALF_LIFE_DAYS_BY_TYPE or
RECENCY_HALF_LIFE_DAYS in app/signal_aggregator.py to propagate the new
half-lives to all stored atoms.

Usage:
    .venv/bin/python scripts/recompute_all_atoms.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.memory_store import MemoryStore

config = get_config()
store = MemoryStore()

with psycopg.connect(config.database_url) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM memory_atoms ORDER BY created_at;")
        atom_ids = [str(row[0]) for row in cur.fetchall()]

total = len(atom_ids)
print(f"Recomputing {total} atom(s)...")

ok = 0
for atom_id in atom_ids:
    result = store.recompute_atom_weights(atom_id)
    if result:
        ok += 1
    else:
        print(f"  WARNING: atom {atom_id} returned None during recompute")

print(f"Done. {ok}/{total} atoms recomputed.")
if ok < total:
    sys.exit(1)
