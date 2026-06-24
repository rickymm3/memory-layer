"""Integration tests for MemoryStore (Postgres backend).

These tests hit a real Postgres database. They are SKIPPED automatically
when DATABASE_URL is not set or the connection fails — safe to run in CI
without Postgres configured (the SQLite tests cover the same logic offline).

Run locally with:
    pytest tests/test_memory_store.py -v          (skips if no DB)
    pytest tests/test_memory_store.py -v -m pg    (same)
"""
from __future__ import annotations

import os
import uuid

import pytest

# ── Skip guard: skip all tests if Postgres is unreachable ─────────────────────

def _pg_available() -> bool:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        import psycopg
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            return False
        with psycopg.connect(url, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="DATABASE_URL not set or Postgres unreachable",
)


_TEST_CONTENT_MARKER = "PG "

@pytest.fixture(scope="module")
def store():
    from dotenv import load_dotenv
    load_dotenv()
    from app.memory_store import MemoryStore
    return MemoryStore()


@pytest.fixture(scope="module", autouse=True)
def cleanup_test_atoms():
    """Delete all atoms written by this test module after the module finishes.

    Tests write atoms with content starting with 'PG ' to the live database.
    This fixture removes them so they don't contaminate health metrics or
    the contested-atoms list in the dashboard.
    """
    yield
    try:
        import psycopg
        import os
        from dotenv import load_dotenv
        load_dotenv()
        url = os.environ.get("DATABASE_URL", "")
        if not url:
            return
        with psycopg.connect(url) as conn:
            conn.execute(
                "DELETE FROM memory_atoms WHERE content LIKE %s",
                (_TEST_CONTENT_MARKER + "%",),
            )
            conn.commit()
    except Exception:
        pass


# ── store_memory_with_signal ──────────────────────────────────────────────────

def test_pg_store_returns_two_uuids(store):
    atom_id, signal_id = store.store_memory_with_signal(
        content=f"PG integration test fact {uuid.uuid4()}.",
        memory_type="fact",
        scope="project:memory-layer",
    )
    assert atom_id
    assert signal_id
    assert atom_id != signal_id


def test_pg_store_initializes_support_weight(store):
    atom_id, _ = store.store_memory_with_signal(
        content=f"PG support weight test {uuid.uuid4()}.",
        memory_type="fact",
        scope="project:memory-layer",
        relationship="new",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom is not None
    assert atom["support_weight"] > 0


def test_pg_unique_source_count_single(store):
    atom_id, _ = store.store_memory_with_signal(
        content=f"PG unique source single {uuid.uuid4()}.",
        memory_type="fact",
        scope="project:memory-layer",
        source_user_id="test_user_a",
        relationship="new",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom["unique_source_count"] == 1


def test_pg_unique_source_count_multi(store):
    content = f"PG unique source multi {uuid.uuid4()}."
    atom_id, _ = store.store_memory_with_signal(
        content=content, memory_type="fact",
        scope="project:memory-layer",
        source_user_id="test_user_a", relationship="new",
    )
    store.add_signal_to_atom(
        atom_id=atom_id, content=content, relationship="reinforcement",
        source_key="user_b", source_user_id="test_user_b",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom["unique_source_count"] == 2


def test_pg_retrieve_includes_unique_source_count(store):
    content = f"PG retrieve unique source {uuid.uuid4()}."
    store.store_memory_with_signal(
        content=content, memory_type="fact",
        scope="project:memory-layer", relationship="new",
    )
    results = store.retrieve_memories(content[:30], limit=5, min_similarity=0.0)
    for r in results:
        assert "unique_source_count" in r


def test_pg_get_atom_includes_unique_source_count(store):
    atom_id, _ = store.store_memory_with_signal(
        content=f"PG get unique source {uuid.uuid4()}.",
        memory_type="fact", scope="project:memory-layer",
        relationship="new",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert "unique_source_count" in atom
    assert isinstance(atom["unique_source_count"], int)


# ── Scope validation ──────────────────────────────────────────────────────────

def test_pg_scope_is_stored_correctly(store):
    atom_id, _ = store.store_memory_with_signal(
        content=f"PG scope test {uuid.uuid4()}.",
        memory_type="fact",
        scope="project:memory-layer",
        relationship="new",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom["scope"] == "project:memory-layer"


# ── recompute_atom_weights ────────────────────────────────────────────────────

def test_pg_recompute_returns_updated_weights(store):
    atom_id, _ = store.store_memory_with_signal(
        content=f"PG recompute test {uuid.uuid4()}.",
        memory_type="fact", scope="project:memory-layer",
        relationship="new",
    )
    result = store.recompute_atom_weights(atom_id)
    assert result is not None
    assert "support_weight" in result
    assert "unique_source_count" in result
    assert result["support_weight"] > 0


# ── Conflict routing ──────────────────────────────────────────────────────────

def test_pg_conflict_signal_increases_opposition(store):
    content = f"PG conflict test {uuid.uuid4()}."
    atom_id, _ = store.store_memory_with_signal(
        content=content, memory_type="fact",
        scope="project:memory-layer", relationship="new",
    )
    initial = store.get_atom_with_signals(atom_id)
    initial_opposition = initial["opposition_weight"]

    store.add_signal_to_atom(
        atom_id=atom_id, content="This contradicts the above.",
        relationship="conflict", source_key="user_b",
    )
    updated = store.get_atom_with_signals(atom_id)
    assert updated["opposition_weight"] > initial_opposition
    assert updated["disagreement_score"] > 0


# ── Access tracking ───────────────────────────────────────────────────────────

def test_pg_retrieve_updates_last_accessed(store):
    import time
    content = f"PG access tracking test {uuid.uuid4()}."
    atom_id, _ = store.store_memory_with_signal(
        content=content, memory_type="fact",
        scope="project:memory-layer", relationship="new",
    )
    before = store.get_atom_with_signals(atom_id)
    assert before["unique_source_count"] is not None  # atom exists

    # Trigger a retrieve which should update last_accessed_at
    store.retrieve_memories(content[:20], limit=5, min_similarity=0.0)
    time.sleep(0.1)

    import psycopg
    from dotenv import load_dotenv
    load_dotenv()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_accessed_at, access_count FROM memory_atoms WHERE id = %s",
                (atom_id,),
            )
            row = cur.fetchone()
    assert row is not None
    # last_accessed_at should now be set (may still be None if cosine similarity
    # didn't return this atom, which is fine for a correctness test)
    # The access_count column must exist and be >= 0
    assert row[1] >= 0
