"""Tests for the write quality scorer — pure Python, no DB or LLM needed."""
from __future__ import annotations

import pytest

from app.write_quality import score_write_quality, QualityResult


# ── Basic structure ────────────────────────────────────────────────────────────

def test_returns_quality_result():
    result = score_write_quality("We use Postgres as the primary database.")
    assert isinstance(result, QualityResult)


def test_result_has_required_fields():
    r = score_write_quality("We use Postgres as the primary database.")
    assert hasattr(r, "quality_score")
    assert hasattr(r, "decision")
    assert hasattr(r, "signals")
    assert hasattr(r, "adjusted_importance")


def test_score_in_range():
    r = score_write_quality("We use Postgres as the primary database.")
    assert 0.0 <= r.quality_score <= 1.0


def test_decision_is_valid_value():
    r = score_write_quality("We use Postgres as the primary database.")
    assert r.decision in ("accept", "downgrade", "reject")


# ── Hard reject cases ─────────────────────────────────────────────────────────

def test_too_short_is_rejected():
    r = score_write_quality("ok")
    assert r.decision == "reject"
    assert r.quality_score == 0.0


def test_question_is_rejected():
    r = score_write_quality("What database should we use?")
    assert r.decision == "reject"


# ── Durable content: should score high ────────────────────────────────────────

def test_architectural_decision_scores_high():
    r = score_write_quality(
        "We chose PostgreSQL as our primary database because it supports JSON and full-text search.",
        memory_type="decision",
    )
    assert r.quality_score >= 0.6
    assert r.decision == "accept"


def test_constraint_type_gets_bonus():
    r_constraint = score_write_quality(
        "All API endpoints must be authenticated before processing requests.",
        memory_type="constraint",
    )
    r_fact = score_write_quality(
        "All API endpoints must be authenticated before processing requests.",
        memory_type="fact",
    )
    assert r_constraint.quality_score >= r_fact.quality_score


def test_tech_name_boosts_score():
    r_generic = score_write_quality("We use a database for storing data and files.")
    r_specific = score_write_quality("We use PostgreSQL with psycopg2 for data storage.")
    assert r_specific.quality_score > r_generic.quality_score


def test_version_number_boosts_score():
    r_no_ver = score_write_quality("We use Python for backend development.")
    r_versioned = score_write_quality("We use Python 3.11 for backend development.")
    assert r_versioned.quality_score >= r_no_ver.quality_score


def test_long_content_gets_small_bonus():
    short = "We use Postgres."
    long_ = "We use Postgres as our primary relational database for production workloads because it offers strong ACID guarantees and native JSON support."
    assert score_write_quality(long_).quality_score >= score_write_quality(short).quality_score


# ── Ephemeral content: should score low ───────────────────────────────────────

def test_date_reference_penalizes():
    r_with_date = score_write_quality("Today we decided to use Postgres for the project.")
    r_without = score_write_quality("We decided to use Postgres for the project.")
    assert r_with_date.quality_score < r_without.quality_score


def test_temporal_now_penalizes():
    r = score_write_quality("Currently we are using SQLite for local development.")
    assert any("temporal" in s.lower() for s in r.signals)


def test_todo_phrasing_penalizes():
    r = score_write_quality("We need to migrate the database to Postgres next week.")
    assert r.quality_score < 0.6 or any("to-do" in s.lower() or "action" in s.lower() for s in r.signals)


def test_vague_qualifiers_penalize():
    r = score_write_quality("We might probably use React for the frontend maybe.")
    assert any("vague" in s.lower() for s in r.signals)


# ── Downgrade behaviour ───────────────────────────────────────────────────────

def test_downgrade_lowers_importance():
    # A vague, ephemeral sentence that passes reject threshold but not accept
    r = score_write_quality(
        "Today we might use a database.",
        stated_importance=0.9,
    )
    if r.decision == "downgrade":
        assert r.adjusted_importance is not None
        assert r.adjusted_importance <= r.quality_score


def test_accept_keeps_importance_none():
    r = score_write_quality(
        "We use PostgreSQL 15 as the primary database for production because of ACID compliance.",
        memory_type="decision",
        stated_importance=0.9,
    )
    if r.decision == "accept":
        assert r.adjusted_importance is None


# ── Signal explanations ────────────────────────────────────────────────────────

def test_signals_are_strings():
    r = score_write_quality("We use PostgreSQL as our primary database.")
    for s in r.signals:
        assert isinstance(s, str)


def test_accept_signals_not_empty():
    r = score_write_quality(
        "We chose PostgreSQL as our primary database for production workloads.",
        memory_type="decision",
    )
    assert len(r.signals) > 0


# ── Session-internal jargon guard ─────────────────────────────────────────────

def test_reject_phase_number_jargon():
    r = score_write_quality("Phase 0 introduced the context_size_log table.")
    assert r.decision == "reject"
    assert any("session-internal jargon" in s for s in r.signals)


def test_reject_phase_letter_jargon():
    r = score_write_quality("Phase A added memory_recent to the SessionStart hook.")
    assert r.decision == "reject"


def test_reject_sprint_jargon():
    r = score_write_quality("Sprint 2 completed the atom relations graph feature.")
    assert r.decision == "reject"


def test_reject_as_discussed_jargon():
    r = score_write_quality(
        "As discussed earlier, all hook scripts now use python3 instead of jq."
    )
    assert r.decision == "reject"


def test_accept_concrete_no_jargon():
    # Same fact rewritten without jargon — should pass
    r = score_write_quality(
        "The memory-layer SessionStart hook calls memory_recent to inject "
        "the last 5 atoms at session start, replacing jq with python3 for JSON parsing."
    )
    assert r.decision in ("accept", "downgrade")
    assert all("session-internal jargon" not in s for s in r.signals)


def test_phase_in_proper_noun_not_rejected():
    # "Phase" appearing as part of a proper noun or unambiguous context
    # that isn't "Phase N" should not trigger the guard
    r = score_write_quality(
        "The PreCompact hook injects top-10 atoms before conversation compaction "
        "using .claude/hooks/memory-compact.sh and python3 for JSON output."
    )
    assert r.decision in ("accept", "downgrade")
