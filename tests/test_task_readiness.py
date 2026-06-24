"""Tests for assess_task_readiness — pure deterministic logic, no DB needed."""
from __future__ import annotations

from app.task_readiness import assess_task_readiness


def _atom(**kwargs) -> dict:
    base = {
        "id": "abc",
        "content": "Some fact.",
        "memory_type": "fact",
        "scope": "project:test",
        "confidence": 0.8,
        "importance": 0.6,
        "disagreement_flag": False,
        "lifecycle_status": "active",
        "similarity": 0.75,
    }
    base.update(kwargs)
    return base


def _ctx(**kwargs) -> dict:
    base = {
        "project_context": [],
        "model_lessons": [],
        "task_relevant_atoms": [],
        "recent_task_runs": [],
    }
    base.update(kwargs)
    return base


def test_returns_required_keys():
    result = assess_task_readiness(
        task_description="anything",
        project_scope="project:test",
        model_scope=None,
        task_context=_ctx(),
    )
    for key in ("ready", "recommended_action", "reasons", "confidence"):
        assert key in result, f"Missing key: {key}"


def test_ready_is_bool():
    result = assess_task_readiness("task", "project:test", None, _ctx())
    assert isinstance(result["ready"], bool)


def test_no_context_not_ready():
    result = assess_task_readiness("unknown task", "project:test", None, _ctx())
    assert result["ready"] is False


def test_strong_context_can_be_ready():
    atoms = [_atom(importance=0.9, confidence=0.95, similarity=0.85) for _ in range(3)]
    ctx = _ctx(project_context=atoms, task_relevant_atoms=atoms)
    result = assess_task_readiness(
        "write a test for the store", "project:test", None, ctx
    )
    assert isinstance(result["ready"], bool)
    assert result["confidence"] >= 0.0


def test_high_disagreement_triggers_inspect_conflict():
    atoms = [_atom(disagreement_flag=True, confidence=0.8, importance=0.8)]
    ctx = _ctx(project_context=atoms, task_relevant_atoms=atoms)
    result = assess_task_readiness("task touching contested area", "project:test", None, ctx)
    reasons_text = " ".join(result.get("reasons", []) + result.get("risks", []))
    # Either recommended_action is inspect_conflict OR reasons mention disagreement
    flagged = (
        result["recommended_action"] == "inspect_conflict"
        or "disagree" in reasons_text.lower()
        or "conflict" in reasons_text.lower()
        or "contested" in reasons_text.lower()
    )
    assert flagged, f"Expected conflict flag, got: {result}"


def test_confidence_float_in_range():
    result = assess_task_readiness("task", "project:test", None, _ctx())
    assert 0.0 <= result["confidence"] <= 1.0
