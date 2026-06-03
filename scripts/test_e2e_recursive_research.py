#!/usr/bin/env python3
"""End-to-end test: RECURSIVE routing path with web research storage.

Steps:
  1. Store a correction atom directly (bypassing pipeline for speed)
  2. Call chat_with_research with a message that retrieves it
  3. Assert route == "recursive"
  4. Wait briefly for background _store_research_bg to finish
  5. Check whether any new web_research atoms were stored in this turn

Cleanup: remove all test artifacts.

Usage:
    .venv/bin/python scripts/test_e2e_recursive_research.py
"""
from __future__ import annotations

import sys
import time
import secrets
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── helpers ───────────────────────────────────────────────────────────────────
_test_atom_ids: list[str] = []
_test_signal_ids: list[str] = []
_results: list[tuple[str, str, str]] = []


def _pass(name: str) -> None:
    _results.append(("PASS", name, ""))
    print(f"  PASS  {name}")


def _fail(name: str, detail: str) -> None:
    _results.append(("FAIL", name, detail))
    print(f"  FAIL  {name}: {detail}")


def _assert(cond: bool, name: str, detail: str = "") -> None:
    if cond:
        _pass(name)
    else:
        _fail(name, detail or "assertion failed")


def _cleanup() -> None:
    import psycopg
    from app.config import get_config
    config = get_config()
    if not any([_test_signal_ids, _test_atom_ids]):
        print("  No DB artifacts to clean up.")
        return
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            for sid in _test_signal_ids:
                cur.execute("DELETE FROM memory_signals WHERE id = %s", (sid,))
            for aid in _test_atom_ids:
                cur.execute("DELETE FROM memory_atoms WHERE id = %s", (aid,))
        conn.commit()
    print(f"  Deleted: {len(_test_signal_ids)} signal(s), {len(_test_atom_ids)} atom(s).")


# ── test ──────────────────────────────────────────────────────────────────────

def run() -> None:
    from app.memory_store import MemoryStore
    from app.chat import chat_with_research
    import psycopg
    from app.config import get_config

    tag = secrets.token_hex(4)
    topic = f"pgvector cosine search [{tag}]"

    print(f"\n  tag: {tag}")

    # Step 1: store a correction atom
    print("\n--- Step 1: store correction atom ---")
    store = MemoryStore()
    atom_id, sig_id = store.store_memory_with_signal(
        content=(
            f"CORRECTION [{tag}]: Always use cosine similarity with pgvector; "
            "do not use L2 distance for high-dimensional text embeddings."
        ),
        memory_type="correction",
        scope="project:memory-layer",
        confidence=0.95,
        importance=0.9,
        relationship="new",
        reconciliation_reason="e2e test fixture",
        source_key="test_e2e",
        source_type="local",
    )
    _test_atom_ids.append(atom_id)
    _test_signal_ids.append(sig_id)
    print(f"  correction atom_id: {atom_id}")
    _pass("correction atom stored")

    # Step 2: call chat_with_research with a message that should retrieve the atom
    print("\n--- Step 2: chat_with_research ---")
    messages = [
        {"role": "user", "content": f"pgvector cosine similarity distance {tag}"}
    ]
    print("  calling chat_with_research... (may take 10-30s)")
    result = chat_with_research(messages)
    answer, atoms, web_results, research_status, context_eval, gap_state, route = result

    print(f"  route: {route}")
    print(f"  research_status: {research_status}")
    print(f"  web_results count: {len(web_results)}")
    print(f"  retrieved atoms: {len(atoms)}")
    print(f"  answer[:120]: {answer[:120]!r}")

    _assert(route == "recursive", "route is recursive", f"got {route!r}")

    # Check if the correction atom was among the retrieved atoms
    retrieved_ids = {str(a.get("id", "")) for a in atoms}
    _assert(atom_id in retrieved_ids, "correction atom was retrieved", f"ids={retrieved_ids}")

    # Step 3: check research
    if research_status == "used":
        _assert(len(web_results) > 0, "web_results non-empty when status=used", f"got {web_results!r}")
    else:
        print(f"  (research not used: {research_status})")
        _pass("research_status noted (provider may be disabled or decided not needed)")

    # Step 4: wait for background store_research_bg to finish
    print("\n--- Step 4: wait for background research storage ---")
    time.sleep(6)

    # Step 5: check DB for any new web_research activity (new atoms OR new signals)
    if research_status == "used" and web_results:
        config = get_config()
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                # New atoms created by this run
                cur.execute(
                    """
                    SELECT id, content, source_url
                    FROM memory_atoms
                    WHERE source_type = 'web_research'
                    AND created_at > now() - interval '3 minutes'
                    ORDER BY created_at DESC
                    LIMIT 5;
                    """,
                )
                new_atoms = cur.fetchall()

                # New web_research signals (covers reinforce_existing path too)
                cur.execute(
                    """
                    SELECT ms.id, ms.memory_atom_id, ms.source_key, ma.source_url
                    FROM memory_signals ms
                    JOIN memory_atoms ma ON ms.memory_atom_id = ma.id
                    WHERE ms.source_type = 'web_research'
                    AND ms.created_at > now() - interval '3 minutes'
                    ORDER BY ms.created_at DESC
                    LIMIT 5;
                    """,
                )
                new_signals = cur.fetchall()

        print(f"  new web_research atoms in last 3 min: {len(new_atoms)}")
        for row in new_atoms:
            aid = str(row[0])
            print(f"    atom {aid}  url={row[2]}")
            _test_atom_ids.append(aid)
            with psycopg.connect(config.database_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM memory_signals WHERE memory_atom_id = %s", (aid,)
                    )
                    for sr in cur.fetchall():
                        _test_signal_ids.append(str(sr[0]))

        print(f"  new web_research signals in last 3 min: {len(new_signals)}")
        for row in new_signals:
            print(f"    sig {str(row[0])[:8]}  atom={str(row[1])[:8]}  url={row[2]}")

        # At least one new atom OR one new signal expected
        _assert(
            len(new_atoms) > 0 or len(new_signals) > 0,
            "background research storage ran (new atom or signal in DB)",
            "no web_research atoms or signals found — pipeline may have rejected all snippets",
        )
        if new_atoms:
            for row in new_atoms:
                _assert(
                    row[2] is not None,
                    f"source_url populated on web_research atom {str(row[0])[:8]}",
                    "source_url is NULL",
                )
    else:
        _pass("research storage check skipped (no web results this turn)")


def main() -> None:
    try:
        run()
    except Exception as exc:
        import traceback
        _fail("e2e run crashed", str(exc))
        traceback.print_exc()
    finally:
        print()
        print("=== cleanup ===")
        _cleanup()

    passes = sum(1 for r in _results if r[0] == "PASS")
    fails = sum(1 for r in _results if r[0] == "FAIL")
    print(f"\nResults: {passes} PASS  {fails} FAIL")
    if fails:
        sys.exit(1)


if __name__ == "__main__":
    main()
