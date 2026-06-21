"""Tests for SQLiteStore — exercises the zero-config SQLite backend end-to-end.

All tests use a temp DB and a deterministic fake embedder so they run offline
and without any external services.
"""
from __future__ import annotations

import json

import pytest

from app.sqlite_store import SQLiteStore
from tests.conftest import _FakeEmbedder

# Threshold that includes ALL atoms (cosine similarity is in [-1, 1])
_ANY = -1.0


# ── store_memory_with_signal ──────────────────────────────────────────────────

def test_store_memory_with_signal_returns_two_ids(store: SQLiteStore):
    atom_id, signal_id = store.store_memory_with_signal(
        content="The embedding model is qwen3-embedding:latest.",
        memory_type="fact",
        scope="project:test",
        confidence=0.9,
        importance=0.7,
    )
    assert atom_id and isinstance(atom_id, str)
    assert signal_id and isinstance(signal_id, str)
    assert atom_id != signal_id


def test_store_memory_with_signal_persists(store: SQLiteStore):
    atom_id, _ = store.store_memory_with_signal(
        content="Postgres rows are the source of truth.",
        memory_type="decision",
        scope="project:memory-layer",
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom is not None
    assert atom["content"] == "Postgres rows are the source of truth."
    assert atom["memory_type"] == "decision"
    assert atom["scope"] == "project:memory-layer"


def test_store_memory_with_signal_recomputes_weights(store: SQLiteStore):
    """A 'reinforcement' signal should push support_weight > 0."""
    atom_id, _ = store.store_memory_with_signal(
        content="Reinforced fact.", memory_type="fact",
        relationship="reinforcement", confidence=0.9,
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom["support_weight"] > 0


# ── retrieve_memories ─────────────────────────────────────────────────────────

def test_retrieve_memories_returns_disagreement_flag(store: SQLiteStore):
    store.store_memory_with_signal(content="Testing disagreement flag.", memory_type="fact")
    # Use _ANY threshold so we get results regardless of cosine direction
    results = store.retrieve_memories("disagreement flag test", limit=10, min_similarity=_ANY)
    assert len(results) >= 1
    for r in results:
        assert "disagreement_flag" in r, "disagreement_flag must be present in retrieve_memories output"
        assert isinstance(r["disagreement_flag"], bool)


def test_retrieve_memories_disagreement_flag_true_when_score_high(store: SQLiteStore):
    """Atoms with disagreement_score >= 0.5 must have flag=True."""
    emb = _FakeEmbedder().embed_text("contested memory")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, context_summary, memory_type, scope, confidence, importance, "
            "embedding_model, embedding, created_at, disagreement_score, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?,?)",
            ("abc123", "contested memory", "contested", "fact", None,
             0.5, 0.5, "fake", json.dumps(emb), 0.9, "active"),
        )
    results = store.retrieve_memories("contested memory", limit=10, min_similarity=_ANY)
    contested = next((r for r in results if r["id"] == "abc123"), None)
    assert contested is not None
    assert contested["disagreement_score"] == pytest.approx(0.9, abs=1e-6)
    assert contested["disagreement_flag"] is True


def test_retrieve_memories_disagreement_flag_false_when_score_low(store: SQLiteStore):
    """Atoms with disagreement_score < 0.5 must have flag=False."""
    emb = _FakeEmbedder().embed_text("settled fact")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, context_summary, memory_type, scope, confidence, importance, "
            "embedding_model, embedding, created_at, disagreement_score, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?,?)",
            ("def456", "settled fact", "settled", "fact", None,
             0.9, 0.8, "fake", json.dumps(emb), 0.1, "active"),
        )
    results = store.retrieve_memories("settled fact", limit=10, min_similarity=_ANY)
    settled = next((r for r in results if r["id"] == "def456"), None)
    assert settled is not None
    assert settled["disagreement_flag"] is False


def test_retrieve_memories_scope_filter_excludes_other_scopes(store: SQLiteStore):
    store.store_memory_with_signal(content="Alpha fact.", memory_type="fact", scope="project:alpha")
    store.store_memory_with_signal(content="Beta fact.", memory_type="fact", scope="project:beta")

    results = store.retrieve_memories("fact", limit=10, min_similarity=_ANY, scope_filter="project:alpha")
    for r in results:
        assert r["scope"] in ("project:alpha", "global", None), \
            f"Got unexpected scope: {r['scope']}"
    assert not any(r["scope"] == "project:beta" for r in results)


def test_retrieve_memories_min_similarity_threshold(store: SQLiteStore):
    store.store_memory_with_signal(content="Completely unrelated topic.", memory_type="fact")
    # Threshold of 1.0 is practically unreachable (cosine similarity < 1 for non-identical vectors)
    results = store.retrieve_memories("completely different query", limit=10, min_similarity=1.0)
    assert results == []


def test_retrieve_memories_archived_atoms_excluded(store: SQLiteStore):
    emb = _FakeEmbedder().embed_text("archived atom content")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, context_summary, memory_type, scope, confidence, importance, "
            "embedding_model, embedding, created_at, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,?,datetime('now'),?)",
            ("archived1", "archived atom content", "archived", "fact", None,
             0.9, 0.9, "fake", json.dumps(emb), "archived"),
        )
    results = store.retrieve_memories("archived atom content", limit=10, min_similarity=_ANY)
    assert not any(r["id"] == "archived1" for r in results)


# ── find_exact_content_match ──────────────────────────────────────────────────

def test_find_exact_content_match_returns_match(store: SQLiteStore):
    content = "Parameterized SQL only. No f-string interpolation."
    atom_id, _ = store.store_memory_with_signal(content=content, memory_type="instruction")
    match = store.find_exact_content_match(content)
    assert match is not None
    assert str(match["id"]) == atom_id


def test_find_exact_content_match_case_insensitive(store: SQLiteStore):
    store.store_memory_with_signal(content="  Signals are immutable.  ", memory_type="fact")
    match = store.find_exact_content_match("signals are immutable.")
    assert match is not None


def test_find_exact_content_match_no_match(store: SQLiteStore):
    store.store_memory_with_signal(content="Known content.", memory_type="fact")
    match = store.find_exact_content_match("Completely different content.")
    assert match is None


# ── list_recent ───────────────────────────────────────────────────────────────

def test_list_recent_returns_atoms(store: SQLiteStore):
    store.store_memory_with_signal(content="Recent fact 1.", memory_type="fact")
    store.store_memory_with_signal(content="Recent fact 2.", memory_type="fact")
    results = store.list_recent(limit=10)
    assert len(results) >= 2


def test_list_recent_scope_filter(store: SQLiteStore):
    store.store_memory_with_signal(content="Scoped atom.", memory_type="fact", scope="project:x")
    store.store_memory_with_signal(content="Other atom.", memory_type="fact", scope="project:y")
    results = store.list_recent(limit=10, scope="project:x")
    assert all(r["scope"] == "project:x" for r in results)


# ── project_context_atoms ─────────────────────────────────────────────────────

def test_project_context_atoms_filters_by_scope(store: SQLiteStore):
    """project_context_atoms must only return atoms for the requested scope."""
    store.store_memory_with_signal(
        content="In-scope high-importance decision.",
        memory_type="decision",
        scope="project:test",
        importance=0.9,
        confidence=0.9,
        relationship="reinforcement",
    )
    store.store_memory_with_signal(
        content="Other-scope atom.",
        memory_type="fact",
        scope="project:other",
        importance=0.9,
        confidence=0.9,
    )
    results = store.project_context_atoms(
        scope="project:test", limit=10, min_importance=0.5, min_confidence=0.4
    )
    contents = [r["content"] for r in results]
    assert "In-scope high-importance decision." in contents
    assert "Other-scope atom." not in contents


def test_project_context_atoms_excludes_low_importance(store: SQLiteStore):
    """Atoms below min_importance should not appear."""
    store.store_memory_with_signal(
        content="Low importance.",
        memory_type="fact",
        scope="project:test",
        importance=0.1,
        confidence=0.9,
    )
    results = store.project_context_atoms(
        scope="project:test", limit=10, min_importance=0.5, min_confidence=0.4
    )
    assert not any(r["content"] == "Low importance." for r in results)


# ── health_stats ──────────────────────────────────────────────────────────────

def test_health_stats_structure(store: SQLiteStore):
    stats = store.health_stats()
    assert "atom_count" in stats
    assert "backend" in stats
    assert stats["backend"] == "sqlite"
    assert isinstance(stats["atom_count"], int)


def test_health_stats_counts_grow(store: SQLiteStore):
    before = store.health_stats()["atom_count"]
    store.store_memory_with_signal(content="Counting atoms.", memory_type="fact")
    after = store.health_stats()["atom_count"]
    assert after == before + 1


# ── get_atom_with_signals ─────────────────────────────────────────────────────

def test_get_atom_with_signals_nonexistent_returns_none(store: SQLiteStore):
    result = store.get_atom_with_signals("00000000-0000-0000-0000-000000000000")
    assert result is None


def test_get_atom_with_signals_includes_signals_summary(store: SQLiteStore):
    atom_id, _ = store.store_memory_with_signal(
        content="Atom with signals.", memory_type="fact"
    )
    atom = store.get_atom_with_signals(atom_id)
    assert atom is not None
    assert "signals_summary" in atom
    assert atom["signals_summary"]["count"] >= 1


# ── search_memories_full ──────────────────────────────────────────────────────

def test_search_memories_full_returns_list(store: SQLiteStore):
    store.store_memory_with_signal(content="Searchable memory about testing.", memory_type="fact")
    results = store.search_memories_full("testing", limit=10)
    assert isinstance(results, list)


def test_search_memories_full_results_have_id_and_content(store: SQLiteStore):
    store.store_memory_with_signal(content="Another searchable memory.", memory_type="fact")
    results = store.search_memories_full("searchable", limit=10)
    for r in results:
        assert "id" in r
        assert "content" in r


# ── composite_score ───────────────────────────────────────────────────────────

def test_retrieve_memories_returns_composite_score(store: SQLiteStore):
    """retrieve_memories results must include composite_score field."""
    store.store_memory_with_signal(content="Composite score test.", memory_type="fact")
    results = store.retrieve_memories("composite score test", limit=5, min_similarity=_ANY)
    assert len(results) >= 1
    for r in results:
        assert "composite_score" in r, "composite_score must be present in retrieve_memories output"
        assert isinstance(r["composite_score"], float)


def test_high_confidence_atom_scores_above_low_confidence(store: SQLiteStore):
    """Atom with higher confidence should rank above atom with lower confidence, all else equal."""
    emb = _FakeEmbedder().embed_text("network configuration")
    import json
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("hi_conf", "network configuration fact", "fact", None,
             0.95, 0.8, "fake", json.dumps(emb), 0.0, 0.9, "active"),
        )
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("lo_conf", "network configuration fact", "fact", None,
             0.1, 0.8, "fake", json.dumps(emb), 0.0, 0.0, "active"),
        )
    results = store.retrieve_memories("network configuration", limit=10, min_similarity=_ANY)
    ids_in_order = [r["id"] for r in results]
    assert "hi_conf" in ids_in_order
    assert "lo_conf" in ids_in_order
    hi_pos = ids_in_order.index("hi_conf")
    lo_pos = ids_in_order.index("lo_conf")
    assert hi_pos < lo_pos, "High-confidence atom should rank above low-confidence atom"


def test_contested_atom_scores_below_uncontested(store: SQLiteStore):
    """Atom with high disagreement_score should rank below uncontested atom with same embedding."""
    emb = _FakeEmbedder().embed_text("contested configuration")
    import json
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("settled", "contested configuration fact", "fact", None,
             0.8, 0.8, "fake", json.dumps(emb), 0.0, 0.8, "active"),
        )
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("disputed", "contested configuration fact", "fact", None,
             0.8, 0.8, "fake", json.dumps(emb), 0.9, 0.8, "active"),
        )
    results = store.retrieve_memories("contested configuration", limit=10, min_similarity=_ANY)
    ids = [r["id"] for r in results]
    assert "settled" in ids and "disputed" in ids
    assert ids.index("settled") < ids.index("disputed"), (
        "Settled atom should rank above highly-contested atom"
    )


# ── get_stale_atoms ───────────────────────────────────────────────────────────

def test_get_stale_atoms_returns_list(store: SQLiteStore):
    results = store.get_stale_atoms()
    assert isinstance(results, list)


def test_get_stale_atoms_contested_atom_appears(store: SQLiteStore):
    """Atom with disagreement_score >= 0.4 should appear in stale results."""
    import json
    emb = _FakeEmbedder().embed_text("contested atom")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("contested1", "contested atom for staleness", "fact", None,
             0.5, 0.5, "fake", json.dumps(emb), 0.8, 0.1, "active"),
        )
    results = store.get_stale_atoms(min_disagreement=0.4)
    assert any(r["id"] == "contested1" for r in results)


def test_get_stale_atoms_staleness_reasons_present(store: SQLiteStore):
    """Each stale atom must include a staleness_reasons list."""
    import json
    emb = _FakeEmbedder().embed_text("stale reasons test")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("stale2", "stale reasons test", "fact", None,
             0.5, 0.5, "fake", json.dumps(emb), 0.9, 0.0, "active"),
        )
    results = store.get_stale_atoms(min_disagreement=0.4)
    flagged = next((r for r in results if r["id"] == "stale2"), None)
    assert flagged is not None
    assert "staleness_reasons" in flagged
    assert isinstance(flagged["staleness_reasons"], list)
    assert len(flagged["staleness_reasons"]) >= 1


# ── find_near_duplicate_pairs ─────────────────────────────────────────────────

def test_find_near_duplicate_pairs_returns_list(store: SQLiteStore):
    results = store.find_near_duplicate_pairs()
    assert isinstance(results, list)


def test_find_near_duplicate_pairs_identical_embeddings_detected(store: SQLiteStore):
    """Two atoms with identical embeddings should be detected as duplicates."""
    import json
    emb = _FakeEmbedder().embed_text("duplicate content")
    with store._connect() as conn:
        for atom_id in ("dup_a", "dup_b"):
            conn.execute(
                "INSERT INTO memory_atoms "
                "(id, content, memory_type, scope, confidence, importance, embedding_model, "
                "embedding, created_at, lifecycle_status) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                (atom_id, f"duplicate content {atom_id}", "fact", None,
                 0.8, 0.8, "fake", json.dumps(emb), "active"),
            )
    pairs = store.find_near_duplicate_pairs(similarity_threshold=0.99)
    found = any(
        (p["atom_a"]["id"] in ("dup_a", "dup_b") and p["atom_b"]["id"] in ("dup_a", "dup_b"))
        for p in pairs
    )
    assert found, "Identical-embedding atoms should appear as a duplicate pair"


def test_find_near_duplicate_pairs_pair_has_required_fields(store: SQLiteStore):
    """Each pair must have similarity, atom_a, and atom_b fields."""
    import json
    emb = _FakeEmbedder().embed_text("field check atom")
    with store._connect() as conn:
        for atom_id in ("fc_a", "fc_b"):
            conn.execute(
                "INSERT INTO memory_atoms "
                "(id, content, memory_type, scope, confidence, importance, embedding_model, "
                "embedding, created_at, lifecycle_status) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
                (atom_id, f"field check atom {atom_id}", "fact", None,
                 0.8, 0.8, "fake", json.dumps(emb), "active"),
            )
    pairs = store.find_near_duplicate_pairs(similarity_threshold=0.99)
    for pair in pairs:
        assert "similarity" in pair
        assert "atom_a" in pair
        assert "atom_b" in pair
        assert "id" in pair["atom_a"]
        assert "content" in pair["atom_a"]


# ── health_report ─────────────────────────────────────────────────────────────

def test_health_report_returns_expected_keys(store: SQLiteStore):
    """health_report() must include all dashboard-required top-level keys."""
    report = store.health_report()
    required = {
        "backend", "total_atoms", "active_atoms", "lifecycle",
        "conflict_rate", "orphan_rate", "contested_count", "orphan_count",
        "avg_confidence", "avg_disagreement", "scope_distribution",
        "type_distribution", "top_contested", "signal_coverage",
        "signal_activity_14d", "graph",
    }
    for key in required:
        assert key in report, f"Missing key: {key}"


def test_health_report_empty_store(store: SQLiteStore):
    """health_report() must not crash on an empty store."""
    report = store.health_report()
    assert report["total_atoms"] == 0
    assert report["active_atoms"] == 0
    assert report["conflict_rate"] == 0.0
    assert report["orphan_rate"] == 0.0


def test_health_report_conflict_rate(store: SQLiteStore):
    """Atoms with disagreement_score >= 0.4 must be counted in contested_count."""
    import json
    emb = _FakeEmbedder().embed_text("contested health atom")
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("health_contested", "contested atom for health test", "fact", "project:test",
             0.5, 0.7, "fake", json.dumps(emb), 0.7, 0.0, "active"),
        )
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, disagreement_score, support_weight, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?,?,?)",
            ("health_clean", "clean atom for health test", "fact", "project:test",
             0.9, 0.8, "fake", json.dumps(emb), 0.1, 1.0, "active"),
        )
    report = store.health_report()
    assert report["contested_count"] >= 1
    assert report["conflict_rate"] > 0.0


def test_health_report_signal_coverage(store: SQLiteStore):
    """signal_coverage.total_signals must match inserted signal count."""
    atom_id, signal_id = store.store_memory_with_signal(
        content="signal coverage test atom",
        memory_type="fact",
        scope="project:test",
        confidence=0.8,
        importance=0.7,
    )
    report = store.health_report()
    assert report["signal_coverage"]["total_signals"] >= 1
    assert report["signal_coverage"]["atoms_with_signals"] >= 1


def test_health_report_type_distribution(store: SQLiteStore):
    """type_distribution must list the memory types of active atoms."""
    store.store_memory_with_signal(
        content="a decision for health report distribution",
        memory_type="decision",
        scope="project:test",
        confidence=0.85,
        importance=0.9,
    )
    report = store.health_report()
    assert "decision" in report["type_distribution"]


# ── atom relations graph ───────────────────────────────────────────────────────

def _insert_atom(store: SQLiteStore, atom_id: str, content: str) -> str:
    """Insert a bare atom row for graph tests — returns atom_id."""
    import json
    emb = _FakeEmbedder().embed_text(content)
    with store._connect() as conn:
        conn.execute(
            "INSERT INTO memory_atoms "
            "(id, content, memory_type, scope, confidence, importance, embedding_model, "
            "embedding, created_at, lifecycle_status) "
            "VALUES (?,?,?,?,?,?,?,?,datetime('now'),?)",
            (atom_id, content, "fact", "project:test",
             0.8, 0.7, "fake", json.dumps(emb), "active"),
        )
    return atom_id


def test_link_atoms_returns_relation_id(store: SQLiteStore):
    """link_atoms() must return a non-empty UUID string."""
    _insert_atom(store, "graph_a", "Postgres is the primary database.")
    _insert_atom(store, "graph_b", "DATABASE_URL must be set for Postgres.")
    rel_id = store.link_atoms("graph_a", "graph_b", relation_type="supports")
    assert rel_id and isinstance(rel_id, str)


def test_link_atoms_invalid_type_raises(store: SQLiteStore):
    """link_atoms() must raise ValueError for unknown relation types."""
    _insert_atom(store, "inv_a", "atom a")
    _insert_atom(store, "inv_b", "atom b")
    with pytest.raises(ValueError, match="Invalid relation_type"):
        store.link_atoms("inv_a", "inv_b", relation_type="invented_type")


def test_get_related_atoms_finds_neighbor(store: SQLiteStore):
    """get_related_atoms() must return the atom linked via link_atoms()."""
    _insert_atom(store, "rel_src", "source atom")
    _insert_atom(store, "rel_tgt", "target atom is related")
    store.link_atoms("rel_src", "rel_tgt", relation_type="related")
    neighbors = store.get_related_atoms("rel_src", depth=1)
    ids = [n["id"] for n in neighbors]
    assert "rel_tgt" in ids


def test_get_related_atoms_bidirectional(store: SQLiteStore):
    """Traversal is bidirectional: querying the target finds the source."""
    _insert_atom(store, "bi_src", "bidirectional source")
    _insert_atom(store, "bi_tgt", "bidirectional target")
    store.link_atoms("bi_src", "bi_tgt", relation_type="supports")
    neighbors = store.get_related_atoms("bi_tgt", depth=1)
    ids = [n["id"] for n in neighbors]
    assert "bi_src" in ids


def test_get_related_atoms_two_hop(store: SQLiteStore):
    """depth=2 traversal should return atoms two hops away."""
    _insert_atom(store, "hop_a", "hop atom A")
    _insert_atom(store, "hop_b", "hop atom B")
    _insert_atom(store, "hop_c", "hop atom C")
    store.link_atoms("hop_a", "hop_b", relation_type="related")
    store.link_atoms("hop_b", "hop_c", relation_type="related")
    neighbors = store.get_related_atoms("hop_a", depth=2)
    ids = [n["id"] for n in neighbors]
    assert "hop_b" in ids
    assert "hop_c" in ids


def test_get_related_atoms_returns_required_fields(store: SQLiteStore):
    """Each neighbor dict must have id, content, relation_type, atom_confidence."""
    _insert_atom(store, "field_src", "field source atom")
    _insert_atom(store, "field_tgt", "field target atom")
    store.link_atoms("field_src", "field_tgt", relation_type="generalizes")
    neighbors = store.get_related_atoms("field_src", depth=1)
    assert neighbors, "Expected at least one neighbor"
    n = neighbors[0]
    for key in ("id", "content", "relation_type", "atom_confidence", "relation_id"):
        assert key in n, f"Missing key: {key}"


def test_get_related_atoms_empty_when_no_links(store: SQLiteStore):
    """An atom with no relations returns an empty list."""
    _insert_atom(store, "lonely", "isolated atom with no relations")
    neighbors = store.get_related_atoms("lonely", depth=1)
    assert neighbors == []


# ── context_size_log / context_efficiency tests ───────────────────────────────

def test_log_context_metrics_returns_id(store: SQLiteStore):
    log_id = store.log_context_metrics(
        session_id="sess-001",
        turn_number=1,
        retrieved_atom_count=5,
        used_atom_count=3,
        retrieved_atom_tokens=200,
        user_tokens=120,
        assistant_tokens=340,
    )
    assert isinstance(log_id, str) and log_id


def test_log_context_metrics_none_session_ok(store: SQLiteStore):
    log_id = store.log_context_metrics(
        session_id=None,
        turn_number=0,
        retrieved_atom_count=0,
        used_atom_count=0,
        retrieved_atom_tokens=0,
        user_tokens=50,
        assistant_tokens=80,
    )
    assert log_id


def test_context_efficiency_returns_expected_keys(store: SQLiteStore):
    report = store.context_efficiency()
    for key in ("avg_retrieval_efficiency", "fat_atoms", "daily_token_trend"):
        assert key in report, f"Missing key: {key}"


def test_context_efficiency_empty_store(store: SQLiteStore):
    report = store.context_efficiency()
    assert report["avg_retrieval_efficiency"] is None
    assert report["fat_atoms"] == []
    assert report["daily_token_trend"] == []


def test_context_efficiency_calculates_avg(store: SQLiteStore):
    store.log_context_metrics("s1", 1, retrieved_atom_count=10, used_atom_count=4,
                              retrieved_atom_tokens=400, user_tokens=100, assistant_tokens=200)
    store.log_context_metrics("s1", 2, retrieved_atom_count=8, used_atom_count=8,
                              retrieved_atom_tokens=320, user_tokens=90, assistant_tokens=180)
    report = store.context_efficiency()
    assert report["avg_retrieval_efficiency"] is not None
    # avg of (4/10) and (8/8) = avg(0.4, 1.0) = 0.7
    assert abs(report["avg_retrieval_efficiency"] - 0.7) < 0.01


def test_context_efficiency_trend_populated(store: SQLiteStore):
    store.log_context_metrics("s1", 1, 3, 1, 120, 80, 160)
    report = store.context_efficiency()
    assert len(report["daily_token_trend"]) >= 1
    day = report["daily_token_trend"][0]
    assert "date" in day
    assert "avg_user_tokens" in day
    assert "avg_retrieved_tokens" in day


# ── Compaction ─────────────────────────────────────────────────────────────────

def test_compact_atoms_to_belief_creates_belief(store: SQLiteStore):
    a1, _ = store.store_memory_with_signal("User prefers concise replies.", memory_type="preference", importance=0.8)
    a2, _ = store.store_memory_with_signal("Get to the point.", memory_type="preference", importance=0.7)
    result = store.compact_atoms_to_belief(
        eligible_ids=[a1, a2],
        auto_deprecate_ids=[],
        belief_content="User strongly prefers concise, direct communication and dislikes verbose responses.",
        scope="user",
        synthesis_reason="Three similar preference atoms expressing the same belief about communication style.",
    )
    assert "belief_atom_id" in result
    assert result["evidence_count"] == 2
    assert result["deprecated_count"] == 0
    assert len(result["relation_ids"]) == 2


def test_compact_transitions_eligible_to_evidence(store: SQLiteStore):
    a1, _ = store.store_memory_with_signal("User likes short answers.", memory_type="preference")
    store.compact_atoms_to_belief(
        eligible_ids=[a1],
        auto_deprecate_ids=[],
        belief_content="User prefers brief responses.",
        scope="user",
        synthesis_reason="test",
    )
    import sqlite3
    with store._connect() as conn:
        row = conn.execute(
            "SELECT lifecycle_status, peak_confidence FROM memory_atoms WHERE id=?", (a1,)
        ).fetchone()
    assert row[0] == "evidence"
    assert row[1] is not None


def test_compact_auto_deprecate_transitions_to_deprecated(store: SQLiteStore):
    a1, _ = store.store_memory_with_signal("Prefers short.", memory_type="preference")
    a2, _ = store.store_memory_with_signal("Short is good.", memory_type="preference")
    store.compact_atoms_to_belief(
        eligible_ids=[a1],
        auto_deprecate_ids=[a2],
        belief_content="User prefers concise communication.",
        scope="user",
        synthesis_reason="test",
    )
    import sqlite3
    with store._connect() as conn:
        row = conn.execute(
            "SELECT lifecycle_status FROM memory_atoms WHERE id=?", (a2,)
        ).fetchone()
    assert row[0] == "deprecated"


def test_frame_historical_atom_includes_frame(store: SQLiteStore):
    atom = {
        "id": "x", "content": "User liked long answers.",
        "lifecycle_status": "deprecated",
        "confidence": 0.75, "support_weight": 3.0,
        "peak_confidence": 0.82, "peak_support_weight": 4.0,
        "lifecycle_updated_at": "2026-01-15T10:00:00",
        "created_at": "2026-01-01T00:00:00",
    }
    framed = store._frame_historical_atom(atom)
    assert "historical_frame" in framed
    assert "deprecated" in framed["historical_frame"]
    assert "0.82" in framed["historical_frame"]
    assert "4.0 reinforcing signals" in framed["historical_frame"]


def test_retrieve_with_history_returns_both_pools(store: SQLiteStore):
    a1, _ = store.store_memory_with_signal("User prefers concise replies.", memory_type="preference")
    store.compact_atoms_to_belief(
        eligible_ids=[a1],
        auto_deprecate_ids=[],
        belief_content="User strongly prefers concise communication.",
        scope="user",
        synthesis_reason="test compaction",
    )
    # retrieve_with_history requires embeddings for cosine search; test the
    # lifecycle transitions directly since Ollama is not available in unit tests.
    with store._connect() as conn:
        a1_status = conn.execute(
            "SELECT lifecycle_status FROM memory_atoms WHERE id=?", (a1,)
        ).fetchone()[0]
        belief_status = conn.execute(
            "SELECT lifecycle_status FROM memory_atoms WHERE memory_type='belief'",
        ).fetchone()[0]
    assert a1_status == "evidence", f"expected 'evidence', got '{a1_status}'"
    assert belief_status == "active"
    # Verify retrieve_with_history returns the correct structure
    result = store.retrieve_with_history("concise", min_similarity=0.0)
    assert "current" in result
    assert "historical" in result
