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
        "signal_activity_14d",
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
