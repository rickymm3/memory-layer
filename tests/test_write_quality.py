"""Tests for the epistemic write classifier — pure Python, no DB or LLM needed.

Philosophy: the classifier never rejects for quality reasons. Every signal has
epistemic value. Only guardrail blocks (secrets, empty, invalid scope) return
decision=reject. Everything else is stored with appropriate type and confidence.
"""
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


# ── Guardrail (hard reject) cases — ONLY these should reject ──────────────────

def test_empty_content_is_rejected():
    r = score_write_quality("ok")
    assert r.decision == "reject"
    assert r.quality_score == 0.0


def test_invalid_scope_rejected_at_guardrail():
    r = score_write_quality("We use Postgres.", scope="ai_training")
    assert r.decision == "reject"


def test_api_key_blocked_by_guardrail():
    r = score_write_quality("Use sk-ant-api03-abc123xyz for auth")
    assert r.decision == "reject"
    assert "guardrail" in r.signals[0]


# ── Questions are stored, not rejected (epistemic philosophy) ─────────────────

def test_question_is_stored_not_rejected():
    r = score_write_quality("What database should we use?")
    assert r.decision != "reject"
    assert r.suggested_memory_type is not None or r.decision in ("accept", "downgrade")


# ── Vague/belief content is stored as belief tier, not rejected ───────────────

def test_vague_content_is_stored():
    r = score_write_quality("The sky might be red maybe.")
    assert r.decision != "reject"


def test_vague_content_suggests_belief_type():
    r = score_write_quality("I think maybe we should use Postgres probably.")
    assert r.decision != "reject"
    assert r.suggested_memory_type in ("belief", None)


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
    assert any("temporal" in s.lower() or "now" in s.lower() or "decay" in s.lower() for s in r.signals)


def test_todo_phrasing_penalizes():
    r = score_write_quality("We need to migrate the database to Postgres next week.")
    assert r.quality_score < 0.6 or any("to-do" in s.lower() or "action" in s.lower() for s in r.signals)


def test_vague_qualifiers_penalize_score_but_not_reject():
    r = score_write_quality("We might probably use React for the frontend maybe.")
    assert r.decision != "reject"
    assert any("belief" in s.lower() or "uncertain" in s.lower() or "vague" in s.lower() for s in r.signals)


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


# ── Scope allowlist ────────────────────────────────────────────────────────────

def test_valid_scope_project_passes():
    r = score_write_quality(
        "We use PostgreSQL as our primary database.", scope="project:memory-layer"
    )
    assert r.decision != "reject" or "scope" not in " ".join(r.signals)


def test_valid_scope_model_passes():
    r = score_write_quality(
        "We use PostgreSQL as our primary database.", scope="model:claude-sonnet-4-6"
    )
    assert r.decision != "reject" or "scope" not in " ".join(r.signals)


def test_valid_scope_user_passes():
    r = score_write_quality("We use PostgreSQL as our primary database.", scope="user")
    assert r.decision != "reject" or "scope" not in " ".join(r.signals)


def test_invalid_scope_rejected():
    r = score_write_quality(
        "We use PostgreSQL as our primary database.", scope="ai_training"
    )
    assert r.decision == "reject"
    assert "invalid scope" in r.signals[0]


def test_bare_project_scope_rejected():
    r = score_write_quality("We use PostgreSQL as our primary database.", scope="project")
    assert r.decision == "reject"


def test_none_scope_is_ok():
    r = score_write_quality("We use PostgreSQL as our primary database.", scope=None)
    assert r.decision != "reject" or "scope" not in " ".join(r.signals)

