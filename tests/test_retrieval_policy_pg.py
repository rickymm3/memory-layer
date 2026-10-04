"""Postgres tests for the retrieval policy: embedding pin, strict scope,
visibility access rule (incl. team membership), importance-aware ranking,
and two-sided conflict reporting.

SKIPPED automatically when DATABASE_URL is not set or Postgres is unreachable.
"""
from __future__ import annotations

import os
import uuid

import pytest


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
            conn.execute("SELECT 1 FROM scope_members LIMIT 0")
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="DATABASE_URL not set, Postgres unreachable, or migration 046 not applied",
)

_MARK = "PGPOLICY "


class _ConstantEmbedder:
    """Every text gets the same vector, so similarity is 1.0 for all rows and
    ranking is decided by the non-similarity terms."""

    def embed_text(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


@pytest.fixture(scope="module")
def store():
    from app.memory_store import MemoryStore
    return MemoryStore(ollama_client=_ConstantEmbedder())


@pytest.fixture
def scope():
    return f"project:pgpolicy-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module", autouse=True)
def cleanup():
    yield
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("DELETE FROM memory_atoms WHERE content LIKE %s", (_MARK + "%",))
        conn.execute("DELETE FROM scope_members WHERE scope LIKE 'project:pgpolicy-%%'")
        conn.commit()


def _ids(results):
    return [r["id"] for r in results]


def _store(store, text, scope, user="alice", visibility="private", importance=0.5):
    atom_id, _ = store.store_memory_with_signal(
        content=_MARK + text,
        scope=scope,
        importance=importance,
        source_user_id=user,
        visibility=visibility,
    )
    return atom_id


def test_atom_records_its_owner(store, scope):
    atom_id = _store(store, "owner is recorded", scope, user="alice")
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        owner = conn.execute(
            "SELECT source_user_id FROM memory_atoms WHERE id = %s", (atom_id,)
        ).fetchone()[0]
    assert owner == "alice"


def test_rows_from_another_embedding_model_are_excluded(store, scope):
    kept = _store(store, "current model row", scope)
    import psycopg
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        # Different model AND different dimension: comparing it would raise.
        foreign = conn.execute(
            "INSERT INTO memory_atoms (content, scope, embedding_model, embedding) "
            "VALUES (%s, %s, 'some-other-embedder', '[1,0,0,0]'::vector) RETURNING id::text",
            (_MARK + "foreign model row", scope),
        ).fetchone()[0]
        conn.commit()
    got = _ids(store.retrieve_memories("q", limit=10, min_similarity=0.0, scope_filter=scope))
    assert kept in got and foreign not in got
    got = _ids(store.search_memories_full("q", limit=10, scope=scope))
    assert kept in got and foreign not in got


def test_null_scope_is_not_a_wildcard(store, scope):
    scoped = _store(store, "scoped row", scope)
    unscoped = _store(store, "unscoped row", None)
    got = _ids(store.retrieve_memories("q", limit=50, min_similarity=0.0, scope_filter=scope))
    assert scoped in got
    assert unscoped not in got


def test_access_rule_private_team_public(store, scope):
    own = _store(store, "alice private", scope, user="alice")
    bobs_private = _store(store, "bob private", scope, user="bob")
    bobs_team = _store(store, "bob team", scope, user="bob", visibility="team")
    bobs_public = _store(store, "bob public", scope, user="bob", visibility="public")

    def visible():
        return set(_ids(store.retrieve_memories(
            "q", limit=50, min_similarity=0.0, scope_filter=scope, requesting_user="alice"
        )))

    got = visible()
    assert own in got and bobs_public in got
    assert bobs_private not in got
    assert bobs_team not in got  # alice is not a member yet

    store.add_scope_member(scope, "alice")
    got = visible()
    assert bobs_team in got
    assert bobs_private not in got  # team membership never exposes private

    # Same rule on the search tool's path and the id-based lookups.
    got = set(_ids(store.search_memories_full("q", limit=20, scope=scope, requesting_user="alice")))
    assert bobs_team in got and bobs_private not in got
    assert store.readable_atom_ids([bobs_private, bobs_team], "alice") == {bobs_team}

    store.remove_scope_member(scope, "alice")
    assert bobs_team not in visible()


def test_importance_breaks_ties(store, scope):
    low = _store(store, "low importance", scope, importance=0.1)
    high = _store(store, "high importance", scope, importance=0.9)
    got = _ids(store.retrieve_memories("q", limit=10, min_similarity=0.0, scope_filter=scope))
    assert got.index(high) < got.index(low)


def test_contested_atom_returns_both_attributed_claims(store, scope):
    atom_id = _store(store, "the team picked Postgres", scope, user="alex", visibility="team")
    store.add_signal_to_atom(
        atom_id=atom_id,
        content=_MARK + "the team picked SQLite",
        relationship="conflict",
        scope=scope,
        source_user_id="jordan",
    )
    store.add_scope_member(scope, "alex")
    results = store.retrieve_memories(
        "q", limit=10, min_similarity=0.0, scope_filter=scope, requesting_user="alex"
    )
    atom = next(r for r in results if r["id"] == atom_id)
    assert atom["disagreement_flag"] is True
    conflict = atom["conflict"]
    assert [c["source"] for c in conflict["supporting"]] == ["alex"]
    assert [c["source"] for c in conflict["opposing"]] == ["jordan"]
    assert "SQLite" in conflict["opposing"][0]["content"]
    assert "Postgres" in conflict["supporting"][0]["content"]


def test_uncontested_atom_has_no_conflict_block(store, scope):
    atom_id = _store(store, "settled fact", scope)
    results = store.retrieve_memories("q", limit=10, min_similarity=0.0, scope_filter=scope)
    atom = next(r for r in results if r["id"] == atom_id)
    assert "conflict" not in atom


def test_exact_match_respects_scope_and_access(store, scope):
    _store(store, "exact dup text", scope, user="bob")
    assert store.find_exact_content_match(_MARK + "exact dup text", scope=scope) is not None
    assert store.find_exact_content_match(_MARK + "exact dup text", scope="project:elsewhere") is None
    assert store.find_exact_content_match(
        _MARK + "exact dup text", scope=scope, requesting_user="alice"
    ) is None
