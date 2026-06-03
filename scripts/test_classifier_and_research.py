#!/usr/bin/env python3
"""Phase 11 tests — routing classifier and research storage.

Part 1 — Unit tests for app.chat._classify_retrieval_signal (no DB, no LLM).
Part 2 — Unit tests for app.research.store_research_findings (no DB, mock pipeline).
Part 3 — Integration smoke test: store_research_findings against live DB.

All DB rows created in Part 3 are deleted in a finally block.

Usage:
    .venv/bin/python scripts/test_classifier_and_research.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

# ── test runner ───────────────────────────────────────────────────────────────
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


# ── Part 1: routing classifier ────────────────────────────────────────────────

def test_classifier_empty_atoms_is_direct() -> None:
    from app.chat import _classify_retrieval_signal
    route = _classify_retrieval_signal([])
    _assert(route == "direct", "empty atoms → direct", f"got {route!r}")


def test_classifier_low_similarity_is_direct() -> None:
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "similarity": 0.20, "memory_type": "fact",
              "lifecycle_status": "active", "disagreement_score": 0.0}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "direct", "low similarity atom → direct", f"got {route!r}")


def test_classifier_clean_atoms_is_context() -> None:
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "similarity": 0.75, "memory_type": "fact",
              "lifecycle_status": "active", "disagreement_score": 0.1}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "context", "clean relevant atom → context", f"got {route!r}")


def test_classifier_correction_triggers_recursive() -> None:
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "similarity": 0.80, "memory_type": "correction",
              "lifecycle_status": "active", "disagreement_score": 0.0}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "recursive", "correction atom → recursive", f"got {route!r}")


def test_classifier_warning_triggers_recursive() -> None:
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "similarity": 0.65, "memory_type": "warning",
              "lifecycle_status": "active", "disagreement_score": 0.0}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "recursive", "warning atom → recursive", f"got {route!r}")


def test_classifier_contested_lifecycle_triggers_recursive() -> None:
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "similarity": 0.70, "memory_type": "fact",
              "lifecycle_status": "contested", "disagreement_score": 0.0}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "recursive", "contested lifecycle → recursive", f"got {route!r}")


def test_classifier_high_disagreement_triggers_recursive() -> None:
    from app.chat import _classify_retrieval_signal, _ROUTE_HIGH_DISAGREEMENT
    atoms = [{"id": "a1", "similarity": 0.70, "memory_type": "fact",
              "lifecycle_status": "active", "disagreement_score": _ROUTE_HIGH_DISAGREEMENT + 0.05}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "recursive", "high disagreement_score → recursive", f"got {route!r}")


def test_classifier_mixed_atoms_recursive_wins() -> None:
    """One clean atom + one correction atom → recursive (trigger wins)."""
    from app.chat import _classify_retrieval_signal
    atoms = [
        {"id": "a1", "similarity": 0.80, "memory_type": "fact",
         "lifecycle_status": "active", "disagreement_score": 0.0},
        {"id": "a2", "similarity": 0.75, "memory_type": "correction",
         "lifecycle_status": "active", "disagreement_score": 0.0},
    ]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "recursive", "clean + correction → recursive", f"got {route!r}")


def test_classifier_no_similarity_field_defaults_to_zero() -> None:
    """Atom with missing similarity field falls back to 0.0 → direct."""
    from app.chat import _classify_retrieval_signal
    atoms = [{"id": "a1", "memory_type": "fact", "lifecycle_status": "active",
              "disagreement_score": 0.0}]
    route = _classify_retrieval_signal(atoms)
    _assert(route == "direct", "missing similarity → direct (treated as 0)", f"got {route!r}")


# ── Part 2: store_research_findings (mock pipeline) ───────────────────────────

class _MockDecision:
    def __init__(self, committed: bool) -> None:
        self._committed = committed

    def to_dict(self) -> dict[str, Any]:
        if self._committed:
            return {
                "committed_atom_id": "mock-atom-id",
                "committed_signal_id": "mock-sig-id",
                "write_action": "commit",
                "decision": "commit",
                "final_memory_text": "Mocked stored text.",
                "reinforces_atom_ids": [],
            }
        return {
            "committed_atom_id": None,
            "write_action": "reinforce_existing",
            "decision": "reinforce_existing",
            "final_memory_text": "Mocked text.",
            "reinforces_atom_ids": ["existing-atom-id"],
        }


class _MockPipeline:
    def __init__(self, committed: bool = True) -> None:
        self._committed = committed

    def commit_candidate(self, _candidate: dict[str, Any]) -> _MockDecision:
        return _MockDecision(self._committed)


def test_store_research_findings_empty_hits() -> None:
    from app.research import store_research_findings
    events = store_research_findings([], "topic", pipeline=_MockPipeline())
    _assert(events == [], "empty hits → empty events list", f"got {events!r}")


def test_store_research_findings_stores_snippets() -> None:
    from app.research import store_research_findings
    hits = [
        {"title": "A", "url": "https://example.com/a", "snippet": "Python was created by Guido."},
        {"title": "B", "url": "https://example.com/b", "snippet": "Released in 1991."},
    ]
    events = store_research_findings(hits, topic="Python origin", pipeline=_MockPipeline())
    _assert(len(events) == 2, "2 hits → 2 stored events", f"got {len(events)}")
    _assert(all(e["event"] == "stored" for e in events), "all events are 'stored'",
            str(events))


def test_store_research_findings_sensitive_snippet_skipped() -> None:
    from app.research import store_research_findings
    hits = [
        {"title": "A", "url": "https://x.com", "snippet": "Set api_key=abc123 in the config."},
    ]
    events = store_research_findings(hits, topic="api key", pipeline=_MockPipeline())
    _assert(events == [], "sensitive snippet skipped", f"got {events!r}")


def test_store_research_findings_empty_snippet_skipped() -> None:
    from app.research import store_research_findings
    hits = [{"title": "A", "url": "https://x.com", "snippet": ""}]
    events = store_research_findings(hits, topic="topic", pipeline=_MockPipeline())
    _assert(events == [], "empty snippet skipped", f"got {events!r}")


def test_store_research_findings_reinforce_path() -> None:
    from app.research import store_research_findings
    hits = [{"title": "A", "url": "https://x.com", "snippet": "Python is great."}]
    events = store_research_findings(hits, topic="Python", pipeline=_MockPipeline(committed=False))
    _assert(len(events) == 1 and events[0]["event"] == "reinforced",
            "reinforce_existing maps to reinforced event", f"got {events!r}")


# ── Part 3: integration smoke test against live DB ────────────────────────────

_test_atom_ids: list[str] = []
_test_signal_ids: list[str] = []


def _cleanup() -> None:
    if not any([_test_signal_ids, _test_atom_ids]):
        print("  No DB artifacts to clean up.")
        return
    import psycopg
    from app.config import get_config
    config = get_config()
    with psycopg.connect(config.database_url) as conn:
        with conn.cursor() as cur:
            for sid in _test_signal_ids:
                cur.execute("DELETE FROM memory_signals WHERE id = %s", (sid,))
            for aid in _test_atom_ids:
                cur.execute("DELETE FROM memory_atoms WHERE id = %s", (aid,))
        conn.commit()
    print(f"  Deleted: {len(_test_signal_ids)} signal(s), {len(_test_atom_ids)} atom(s).")


def test_store_research_findings_live_db() -> None:
    """store_research_findings with a unique snippet stores a real atom in DB."""
    import secrets as _secrets
    from app.research import store_research_findings
    from mcp_server.tools.get import get_memory_by_id

    run_tag = _secrets.token_hex(4)
    hits = [{
        "title": "Test Research Result",
        "url": "https://test.example.com/research",
        "snippet": f"TEST ONLY [{run_tag}]: research storage integration test snippet.",
    }]
    events = store_research_findings(hits, topic=f"test topic [{run_tag}]", scope="test")

    if not events:
        # Pipeline may reject low-value snippets — acceptable for integration test
        print(f"  (pipeline rejected or reinforced test snippet — acceptable)")
        _pass("store_research_findings live DB: pipeline ran without error")
        return

    event = events[0]
    if event["event"] == "stored" and event.get("memory_atom_id"):
        aid = event["memory_atom_id"]
        _test_atom_ids.append(aid)
        if event.get("memory_signal_id"):
            _test_signal_ids.append(event["memory_signal_id"])
        atom = get_memory_by_id(aid)
        _assert(atom is not None, "stored atom is retrievable via get_memory_by_id",
                f"atom_id={aid}")
        if atom:
            _assert(atom.get("scope") == "test",
                    "stored atom has correct scope",
                    f"got scope={atom.get('scope')!r}")
        _pass("store_research_findings live DB: atom stored and retrievable")
    elif event["event"] == "reinforced":
        _pass("store_research_findings live DB: reinforced existing (acceptable)")
    else:
        _fail("store_research_findings live DB: unexpected event", str(event))


# ── runner ────────────────────────────────────────────────────────────────────

def _run_all() -> None:
    print("\n=== Part 1: routing classifier ===")
    test_classifier_empty_atoms_is_direct()
    test_classifier_low_similarity_is_direct()
    test_classifier_clean_atoms_is_context()
    test_classifier_correction_triggers_recursive()
    test_classifier_warning_triggers_recursive()
    test_classifier_contested_lifecycle_triggers_recursive()
    test_classifier_high_disagreement_triggers_recursive()
    test_classifier_mixed_atoms_recursive_wins()
    test_classifier_no_similarity_field_defaults_to_zero()

    print("\n=== Part 2: store_research_findings (mock pipeline) ===")
    test_store_research_findings_empty_hits()
    test_store_research_findings_stores_snippets()
    test_store_research_findings_sensitive_snippet_skipped()
    test_store_research_findings_empty_snippet_skipped()
    test_store_research_findings_reinforce_path()

    print("\n=== Part 3: integration smoke test ===")
    test_store_research_findings_live_db()


def main() -> None:
    try:
        _run_all()
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
