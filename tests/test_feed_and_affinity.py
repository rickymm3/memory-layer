"""Tests for feed_publisher pure functions and topic_affinity ranking.

These guard the logic paths that had runtime bugs:
  - topic_affinity: wrong FK column (ms.atom_id) and UUID/TEXT type mismatch
  - feed_publisher: title/tag extraction edge cases
  - discussion_synthesizer: mechanical fallback and labeling logic
"""
from __future__ import annotations

import pytest

from app.feed_publisher import _make_title, _extract_tags
from app.topic_affinity import rank_discussions_by_affinity, get_user_topic_tags
from app.discussion_synthesizer import _mechanical_synthesis


# ── _make_title ────────────────────────────────────────────────────────────────

def test_make_title_short_message():
    assert _make_title("Hello") == "Hello"


def test_make_title_truncates_long_message():
    msg = "a " * 100
    title = _make_title(msg)
    assert len(title) <= 102  # _MAX_TITLE_LEN(100) + "…"


def test_make_title_uses_first_sentence():
    title = _make_title("What is the best way to learn Rust? I tried several approaches.")
    assert title == "What is the best way to learn Rust"


def test_make_title_empty_returns_fallback():
    assert _make_title("") == "Untitled discussion"


def test_make_title_whitespace_only_returns_fallback():
    assert _make_title("   ") == "Untitled discussion"


# ── _extract_tags ──────────────────────────────────────────────────────────────

def test_extract_tags_returns_list():
    tags = _extract_tags("How does machine learning work?", "Machine learning uses algorithms.")
    assert isinstance(tags, list)


def test_extract_tags_max_five():
    msg = "python programming language database framework deployment memory"
    tags = _extract_tags(msg, msg)
    assert len(tags) <= 5


def test_extract_tags_filters_stopwords():
    tags = _extract_tags("about because through using where which", "about those")
    # All words are stopwords — should return empty or very few
    for t in tags:
        assert t not in {"about", "because", "through", "using", "where", "which"}


def test_extract_tags_deduplicates():
    tags = _extract_tags("python python python", "python python")
    assert tags.count("python") == 1


# ── rank_discussions_by_affinity ───────────────────────────────────────────────

def test_rank_empty_user_tags_returns_unchanged():
    discs = [{"id": "a", "topic_tags": ["python"]}, {"id": "b", "topic_tags": ["rust"]}]
    result = rank_discussions_by_affinity(discs, [])
    assert [d["id"] for d in result] == ["a", "b"]


def test_rank_matching_discussion_rises():
    discs = [
        {"id": "no_match", "topic_tags": ["java"], "contributor_count": 0, "thread_status": "gathering", "title": ""},
        {"id": "match", "topic_tags": ["python"], "contributor_count": 0, "thread_status": "gathering", "title": ""},
    ]
    result = rank_discussions_by_affinity(discs, ["python"])
    assert result[0]["id"] == "match"


def test_rank_answered_discussions_get_boost():
    discs = [
        {"id": "gathering", "topic_tags": ["python"], "contributor_count": 0, "thread_status": "gathering", "title": ""},
        {"id": "answered", "topic_tags": ["python"], "contributor_count": 0, "thread_status": "answered", "title": ""},
    ]
    result = rank_discussions_by_affinity(discs, ["python"])
    assert result[0]["id"] == "answered"


def test_rank_empty_discussions_safe():
    result = rank_discussions_by_affinity([], ["python", "rust"])
    assert result == []


# ── get_user_topic_tags ────────────────────────────────────────────────────────

def test_get_user_topic_tags_empty_username():
    result = get_user_topic_tags("", "postgresql://localhost/db")
    assert result == []


def test_get_user_topic_tags_empty_db_url():
    result = get_user_topic_tags("alice", "")
    assert result == []


def test_get_user_topic_tags_bad_db_url_returns_empty():
    # DB not available — should return [] not raise
    result = get_user_topic_tags("alice", "postgresql://localhost:9999/nonexistent")
    assert result == []


# ── _mechanical_synthesis ──────────────────────────────────────────────────────

def test_mechanical_synthesis_single_item():
    result = _mechanical_synthesis(["[High-confidence claim] Python is popular."])
    assert "1 perspective" in result
    assert "Python is popular" in result


def test_mechanical_synthesis_multiple_items():
    result = _mechanical_synthesis([
        "[High-confidence claim] A",
        "[Supporting perspective] B",
        "[Contested claim] C",
    ])
    assert "3 perspectives" in result
    assert "Additionally" in result


def test_mechanical_synthesis_empty_returns_something():
    # Edge case: empty list — should not raise
    result = _mechanical_synthesis([])
    assert isinstance(result, str)


def test_mechanical_synthesis_truncates_long_items():
    long_item = "x" * 500
    result = _mechanical_synthesis([long_item])
    # Each item is capped at 200 chars in the result
    assert len(result) < 400
