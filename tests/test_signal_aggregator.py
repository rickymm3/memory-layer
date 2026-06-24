"""Tests for signal_aggregator — pure Python, no DB or LLM needed."""
from __future__ import annotations

from datetime import datetime, timezone

from app.signal_aggregator import compute_atom_weights, AGREEING, CONFLICTING


def _sig(relationship: str, confidence: float = 0.8, created_at=None) -> dict:
    return {
        "relationship": relationship,
        "confidence": confidence,
        "source_key": "test",
        "created_at": created_at,
    }


def test_agreeing_set_contains_expected_relationships():
    assert "reinforcement" in AGREEING
    assert "new" in AGREEING
    assert "refinement" in AGREEING


def test_conflicting_set_contains_expected_relationships():
    assert "conflict" in CONFLICTING
    assert "opinion_change" in CONFLICTING


def test_single_reinforcement_signal():
    result = compute_atom_weights([_sig("reinforcement")])
    assert result["support_weight"] > 0
    assert result["opposition_weight"] == 0.0
    assert result["disagreement_score"] < 0.5


def test_single_conflict_signal():
    result = compute_atom_weights([_sig("conflict")])
    assert result["opposition_weight"] > 0
    assert result["support_weight"] == 0.0


def test_conflicting_signals_raise_disagreement():
    signals = [_sig("reinforcement", 0.9), _sig("conflict", 0.9)]
    result = compute_atom_weights(signals)
    assert result["disagreement_score"] > 0.0


def test_pure_support_has_low_disagreement():
    signals = [_sig("reinforcement") for _ in range(5)]
    result = compute_atom_weights(signals)
    assert result["disagreement_score"] < 0.3


def test_result_has_expected_keys():
    result = compute_atom_weights([_sig("reinforcement")])
    for key in ("support_weight", "opposition_weight", "disagreement_score", "confidence", "retrieval_priority"):
        assert key in result, f"Missing key: {key}"


def test_retrieval_priority_high_confidence_low_disagreement():
    """High-confidence, uncontested atom should have retrieval_priority > 0.7."""
    signals = [_sig("reinforcement", 0.95) for _ in range(3)]
    result = compute_atom_weights(signals)
    assert result["retrieval_priority"] > 0.7


def test_retrieval_priority_low_for_contested():
    """Highly contested atom should have lower retrieval_priority than uncontested one."""
    uncontested = compute_atom_weights([_sig("reinforcement", 0.9), _sig("reinforcement", 0.9)])
    contested = compute_atom_weights([_sig("reinforcement", 0.9), _sig("conflict", 0.9)])
    assert contested["retrieval_priority"] < uncontested["retrieval_priority"]


def test_empty_signals_returns_defaults():
    result = compute_atom_weights([])
    assert result["support_weight"] == 0.0
    assert result["opposition_weight"] == 0.0
    assert result["disagreement_score"] == 0.0


def test_unrecognized_relationship_is_ignored():
    result = compute_atom_weights([_sig("unknown_rel")])
    assert result["support_weight"] == 0.0
    assert result["opposition_weight"] == 0.0


def test_recency_weight_handles_iso_string():
    """Signals with ISO string timestamps (from SQLite) must not crash."""
    now_str = datetime.now(timezone.utc).isoformat()
    result = compute_atom_weights([_sig("reinforcement", created_at=now_str)])
    assert result["support_weight"] > 0


def test_recency_weight_handles_none():
    result = compute_atom_weights([_sig("reinforcement", created_at=None)])
    assert result["support_weight"] > 0
