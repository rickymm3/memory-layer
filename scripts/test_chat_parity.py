#!/usr/bin/env python3
"""Parity test: verify dashboard chat and MCP use the same memory store.

Tests the write and retrieval paths without making LLM calls.
Writes atoms through the same functions the dashboard chat and MCP tools use,
then verifies cross-retrieval.  All test artifacts are cleaned up on exit.

Usage:
    .venv/bin/python scripts/test_chat_parity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.memory_store import MemoryStore
from mcp_server.tools.search import search_memories
from mcp_server.tools.store_auto import store_memory_auto

_SCOPE = "project:questledger"
_test_atom_ids: list[str] = []
_test_signal_ids: list[str] = []


def _cleanup() -> None:
    config = get_config()
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            for sid in _test_signal_ids:
                cur.execute("DELETE FROM memory_signals WHERE id = %s", (sid,))
            for aid in _test_atom_ids:
                cur.execute("DELETE FROM memory_atoms WHERE id = %s", (aid,))
        conn.commit()
    print(f"  Cleaned up: {len(_test_atom_ids)} atom(s), {len(_test_signal_ids)} signal(s).")


def main() -> int:
    print("=== Chat / MCP parity test ===\n")
    ok = True

    try:
        # ── Test 1: Dashboard chat write → MCP retrieval ─────────────────────
        print("Test 1: dashboard chat write path → MCP search retrieval")

        # store_memory_auto is what dashboard chat calls through extract_and_store_from_message
        report1 = store_memory_auto(
            content="QuestLedger uses event sourcing for all ledger entry mutations.",
            memory_type="decision",
            relationship="new",
            context_summary="QuestLedger uses event sourcing for ledger entries.",
            scope=_SCOPE,
            confidence=0.85,
            importance=0.75,
        )
        assert report1.get("stored"), f"store_memory_auto failed: {report1}"
        atom_id_1 = report1["memory_atom_id"]
        signal_id_1 = report1["memory_signal_id"]
        _test_atom_ids.append(atom_id_1)
        _test_signal_ids.append(signal_id_1)
        print(f"  Stored  atom_id={atom_id_1[:8]}  signal_id={signal_id_1[:8]}  ✓")

        # MCP search_memories (same function called by memory_search MCP tool)
        mcp_results = search_memories(
            query="QuestLedger ledger event sourcing",
            scope=_SCOPE,
            limit=10,
        )
        found_by_mcp = any(r.get("id") == atom_id_1 for r in mcp_results)
        assert found_by_mcp, f"MCP search did not find atom {atom_id_1[:8]}"
        print(f"  MCP search found atom {atom_id_1[:8]}  ✓")

        # ── Test 2: MCP write → Dashboard chat retrieval ─────────────────────
        print("\nTest 2: MCP write path → dashboard chat retrieval (MemoryStore)")

        report2 = store_memory_auto(
            content="QuestLedger is a personal finance application built with Python and PostgreSQL.",
            memory_type="fact",
            relationship="new",
            context_summary="QuestLedger: personal finance app, Python + PostgreSQL.",
            scope=_SCOPE,
            confidence=0.88,
            importance=0.70,
        )
        assert report2.get("stored"), f"store_memory_auto failed: {report2}"
        atom_id_2 = report2["memory_atom_id"]
        signal_id_2 = report2["memory_signal_id"]
        _test_atom_ids.append(atom_id_2)
        _test_signal_ids.append(signal_id_2)
        print(f"  Stored  atom_id={atom_id_2[:8]}  signal_id={signal_id_2[:8]}  ✓")

        # MemoryStore.retrieve_memories is what the /chat route uses
        store = MemoryStore()
        dashboard_results = store.retrieve_memories(
            "QuestLedger Python finance PostgreSQL",
            limit=10,
        )
        found_by_dashboard = any(r.get("id") == atom_id_2 for r in dashboard_results)
        assert found_by_dashboard, (
            f"Dashboard retrieve_memories did not find atom {atom_id_2[:8]}. "
            f"Got: {[r.get('id', '')[:8] for r in dashboard_results]}"
        )
        print(f"  Dashboard retrieve_memories found atom {atom_id_2[:8]}  ✓")

        # ── Test 3: Both writes have atom + linked signal ─────────────────────
        print("\nTest 3: both writes created memory_atom + linked memory_signal")
        config = get_config()
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                for atom_id, signal_id, label in [
                    (atom_id_1, signal_id_1, "dashboard-chat write"),
                    (atom_id_2, signal_id_2, "MCP write"),
                ]:
                    cur.execute("SELECT id FROM memory_atoms WHERE id = %s", (atom_id,))
                    assert cur.fetchone() is not None, f"memory_atom missing for {label}"

                    cur.execute(
                        "SELECT id FROM memory_signals WHERE id = %s AND memory_atom_id = %s",
                        (signal_id, atom_id),
                    )
                    assert cur.fetchone() is not None, f"memory_signal missing for {label}"
                    print(f"  {label}: atom ✓  signal ✓")

        # ── Test 4: No separate chat-only store ────────────────────────────────
        print("\nTest 4: no separate chat-only memory table")
        with psycopg.connect(config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND (table_name LIKE '%chat%'
                           OR table_name LIKE '%message%'
                           OR table_name LIKE '%conversation%')
                    ORDER BY table_name;
                    """
                )
                chat_tables = [r[0] for r in cur.fetchall()]
        assert len(chat_tables) == 0, f"Unexpected chat-only tables: {chat_tables}"
        print("  No chat-only tables found  ✓")

        # ── Test 5: Cross-direction confirm ───────────────────────────────────
        print("\nTest 5: cross-direction — both atoms retrievable from both paths")
        for atom_id, label in [(atom_id_1, "atom1"), (atom_id_2, "atom2")]:
            # via MCP
            mcp_r = search_memories(query="QuestLedger", scope=_SCOPE, limit=20)
            assert any(r.get("id") == atom_id for r in mcp_r), \
                f"{label} not found via MCP search"
            # via dashboard store
            dash_r = store.retrieve_memories("QuestLedger", limit=20)
            assert any(r.get("id") == atom_id for r in dash_r), \
                f"{label} not found via MemoryStore.retrieve_memories"
        print("  Both atoms visible from both retrieval paths  ✓")

    except AssertionError as exc:
        print(f"\n  FAIL: {exc}", file=sys.stderr)
        ok = False
    except Exception as exc:
        print(f"\n  ERROR: {exc}", file=sys.stderr)
        ok = False
    finally:
        print("\n=== cleanup ===")
        _cleanup()

    if ok:
        print("\nAll parity assertions passed.")
        return 0
    else:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
