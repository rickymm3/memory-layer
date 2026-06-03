"""Tests for the Runtime Context Evaluator.

Uses deterministic inputs (no LLM required for most tests) to verify pre-filter
logic, risk gate, and DB trace storage. Integration tests use a StubLLM.

Run with: python scripts/test_context_evaluator.py
"""
from __future__ import annotations

import json
import sys
import traceback
from typing import Any

import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.context_evaluator import (
    ALLOWED_STATUSES,
    ALLOWED_ACTIONS,
    ContextEvaluation,
    RuntimeContextEvaluator,
    _apply_risk_gate,
    _deterministic_pre_filter,
    _parse_critic_response,
)

_config = get_config()
_pass = 0
_fail = 0
_warn = 0


def _p(msg: str) -> None:
    print(msg)


def _pass_test(name: str, detail: str = "") -> None:
    global _pass
    _pass += 1
    suffix = f" — {detail}" if detail else ""
    _p(f"  PASS  {name}{suffix}")


def _fail_test(name: str, detail: str = "") -> None:
    global _fail
    _fail += 1
    suffix = f" — {detail}" if detail else ""
    _p(f"  FAIL  {name}{suffix}")


def _warn_test(name: str, detail: str = "") -> None:
    global _warn
    _warn += 1
    suffix = f" — {detail}" if detail else ""
    _p(f"  WARN  {name}{suffix}")


def _cleanup_trace(trace_id: str | None) -> None:
    if not trace_id:
        return
    try:
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM runtime_context_traces WHERE id = %s::uuid;",
                    (trace_id,),
                )
            conn.commit()
    except Exception:
        pass


# ── Atom fixtures ─────────────────────────────────────────────────────────────

def _atom(
    atom_id: str = "00000000-0000-0000-0000-000000000001",
    lifecycle: str = "active",
    confidence: float = 0.85,
    similarity: float = 0.80,
    disagreement: float = 0.0,
    scope: str | None = None,
    created_at: str | None = "2025-01-01T00:00:00",
) -> dict[str, Any]:
    return {
        "id": atom_id,
        "content": "Test memory content.",
        "context_summary": "Test memory.",
        "memory_type": "instruction",
        "scope": scope,
        "confidence": confidence,
        "importance": 0.7,
        "similarity": similarity,
        "created_at": created_at,
        "lifecycle_status": lifecycle,
        "support_weight": 1.0,
        "opposition_weight": 0.0,
        "disagreement_score": disagreement,
    }


# ── Stub LLM ──────────────────────────────────────────────────────────────────

class _StubLLM:
    def __init__(self, responses: list[str]) -> None:
        self._queue = list(responses)

    def generate_response(self, prompt: str, system: str | None = None) -> str:
        return self._queue.pop(0) if self._queue else json.dumps({
            "context_status": "sufficient",
            "confidence": 0.8,
            "used_atom_ids": [],
            "ignored_atom_ids": [],
            "additional_issues": [],
            "final_action": "answer",
        })

    def embed_text(self, text: str) -> list[float]:
        return [0.1] * 4096


def _critic_json(
    status: str = "sufficient",
    confidence: float = 0.85,
    used_ids: list[str] | None = None,
    ignored: list[dict] | None = None,
    additional_issues: list[dict] | None = None,
    action: str = "answer",
) -> str:
    return json.dumps({
        "context_status": status,
        "confidence": confidence,
        "used_atom_ids": used_ids or [],
        "ignored_atom_ids": ignored or [],
        "additional_issues": additional_issues or [],
        "final_action": action,
        "reasoning_summary": "stub",
    })


# ── Unit tests: pre-filter ────────────────────────────────────────────────────

def test_prefilter_active_atom_passes() -> None:
    atoms = [_atom(lifecycle="active", similarity=0.9)]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert len(result["usable"]) == 1
    assert not result["ignored"]
    _pass_test("pre-filter: active high-confidence atom passes through")


def test_prefilter_superseded_atom_ignored() -> None:
    atoms = [_atom(lifecycle="superseded")]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert len(result["usable"]) == 0
    assert result["ignored"][0]["reason"] == "lifecycle_superseded"
    _pass_test("pre-filter: superseded atom is ignored")


def test_prefilter_deprecated_atom_ignored() -> None:
    atoms = [_atom(lifecycle="deprecated")]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert not result["usable"]
    assert result["ignored"][0]["reason"] == "lifecycle_deprecated"
    _pass_test("pre-filter: deprecated atom is ignored")


def test_prefilter_archived_atom_ignored() -> None:
    atoms = [_atom(lifecycle="archived")]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert not result["usable"]
    assert result["ignored"][0]["reason"] == "lifecycle_archived"
    _pass_test("pre-filter: archived atom is ignored")


def test_prefilter_low_similarity_ignored() -> None:
    atoms = [_atom(similarity=0.20)]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert not result["usable"]
    assert result["ignored"][0]["reason"] == "low_relevance"
    _pass_test("pre-filter: low-similarity atom is ignored (low_relevance)")


def test_prefilter_scope_mismatch_ignored() -> None:
    atoms = [_atom(scope="project-a", similarity=0.90)]
    result = _deterministic_pre_filter(atoms, current_scope="project-b")
    assert not result["usable"]
    assert result["ignored"][0]["reason"] == "scope_mismatch"
    _pass_test("pre-filter: scope-mismatch atom is ignored")


def test_prefilter_global_scope_not_ignored() -> None:
    atoms = [_atom(scope="global", similarity=0.90)]
    result = _deterministic_pre_filter(atoms, current_scope="project-b")
    assert len(result["usable"]) == 1, "global scope should not be ignored by scope filter"
    _pass_test("pre-filter: global-scope atom passes scope filter")


def test_prefilter_contested_flagged_as_issue() -> None:
    atoms = [_atom(lifecycle="contested", similarity=0.90)]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert len(result["usable"]) == 1, "contested atom should be usable"
    issue_types = [i["type"] for i in result["issues"]]
    assert "contested" in issue_types
    _pass_test("pre-filter: contested atom is usable but flagged as issue")


def test_prefilter_high_disagreement_flagged() -> None:
    atoms = [_atom(disagreement=0.75)]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert result["usable"]
    issue_types = [i["type"] for i in result["issues"]]
    assert "conflicting_signals" in issue_types
    _pass_test("pre-filter: high-disagreement atom is flagged with conflicting_signals")


def test_prefilter_low_confidence_flagged() -> None:
    atoms = [_atom(confidence=0.25)]
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert result["usable"]
    issue_types = [i["type"] for i in result["issues"]]
    assert "low_confidence" in issue_types
    _pass_test("pre-filter: low-confidence atom is flagged")


def test_prefilter_stale_atom_flagged() -> None:
    atoms = [_atom(created_at="2022-01-01T00:00:00")]  # more than 180 days ago
    result = _deterministic_pre_filter(atoms, current_scope=None)
    assert result["usable"]
    issue_types = [i["type"] for i in result["issues"]]
    assert "possibly_stale" in issue_types
    _pass_test("pre-filter: old atom flagged as possibly_stale")


# ── Unit tests: risk gate ─────────────────────────────────────────────────────

def test_risk_gate_no_usable_atoms_insufficient() -> None:
    critic = {"context_status": "sufficient", "confidence": 0.9, "final_action": "answer", "additional_issues": []}
    status, conf, action = _apply_risk_gate(0, [], critic)
    assert status == "insufficient", f"expected insufficient, got {status}"
    assert action == "answer_with_caveat"
    _pass_test("risk_gate: no usable atoms → insufficient + answer_with_caveat")


def test_risk_gate_unsafe_forces_refuse() -> None:
    critic = {"context_status": "unsafe_or_blocked", "confidence": 0.5, "final_action": "answer", "additional_issues": []}
    status, conf, action = _apply_risk_gate(2, [], critic)
    assert status == "unsafe_or_blocked"
    assert action == "refuse"
    _pass_test("risk_gate: unsafe_or_blocked → refuse")


def test_risk_gate_low_confidence_upgrades_to_caveat() -> None:
    critic = {"context_status": "sufficient", "confidence": 0.35, "final_action": "answer", "additional_issues": []}
    status, conf, action = _apply_risk_gate(2, [], critic)
    assert action == "answer_with_caveat", f"expected answer_with_caveat, got {action}"
    _pass_test("risk_gate: confidence < 0.5 upgrades answer → answer_with_caveat")


def test_risk_gate_conflicting_issues_lower_confidence() -> None:
    issues = [
        {"type": "conflicting_signals", "severity": "medium"},
        {"type": "contested", "severity": "medium"},
    ]
    critic = {"context_status": "sufficient", "confidence": 0.85, "final_action": "answer", "additional_issues": []}
    _, conf, _ = _apply_risk_gate(3, issues, critic)
    assert conf < 0.85, f"confidence should have been reduced, got {conf}"
    _pass_test("risk_gate: conflicting issues reduce confidence")


def test_risk_gate_many_conflicts_change_status() -> None:
    issues = [
        {"type": "conflicting_signals", "severity": "medium"},
        {"type": "contested", "severity": "medium"},
        {"type": "conflicting_signals", "severity": "high"},
    ]
    critic = {"context_status": "sufficient", "confidence": 0.85, "final_action": "answer", "additional_issues": []}
    status, _, _ = _apply_risk_gate(3, issues, critic)
    assert status == "conflicting", f"expected conflicting, got {status}"
    _pass_test("risk_gate: ≥2 conflict issues change status to conflicting")


def test_risk_gate_verify_then_answer_passes_through() -> None:
    critic = {"context_status": "sufficient", "confidence": 0.75, "final_action": "verify_then_answer", "additional_issues": []}
    _, _, action = _apply_risk_gate(2, [], critic)
    assert action == "verify_then_answer"
    _pass_test("risk_gate: verify_then_answer passes through unchanged")


# ── Unit tests: critic parser ─────────────────────────────────────────────────

def test_parse_critic_valid_json() -> None:
    ids = {"id-1", "id-2"}
    raw = _critic_json(status="sufficient", confidence=0.9, used_ids=["id-1", "id-2"], action="answer")
    result = _parse_critic_response(raw, ids, 0.5)
    assert result["context_status"] == "sufficient"
    assert set(result["used_atom_ids"]) == ids
    _pass_test("parse_critic_response: valid JSON parsed correctly")


def test_parse_critic_unknown_status_normalised() -> None:
    ids = {"id-1"}
    raw = json.dumps({"context_status": "made_up", "confidence": 0.7, "used_atom_ids": ["id-1"],
                      "ignored_atom_ids": [], "additional_issues": [], "final_action": "answer"})
    result = _parse_critic_response(raw, ids, 0.5)
    assert result["context_status"] == "insufficient"
    _pass_test("parse_critic_response: unknown status normalised to insufficient")


def test_parse_critic_invalid_ids_filtered() -> None:
    ids = {"id-real"}
    raw = _critic_json(used_ids=["id-real", "id-fake-not-in-retrieved"])
    result = _parse_critic_response(raw, ids, 0.5)
    assert "id-fake-not-in-retrieved" not in result["used_atom_ids"]
    _pass_test("parse_critic_response: atom IDs not in valid set are filtered out")


def test_parse_critic_invalid_json_fallback() -> None:
    ids = {"id-1", "id-2"}
    result = _parse_critic_response("not json at all !!!", ids, 0.7)
    assert result["final_action"] == "answer_with_caveat"
    _pass_test("parse_critic_response: invalid JSON falls back to answer_with_caveat with all atoms")


# ── Unit tests: ContextEvaluation ─────────────────────────────────────────────

def test_evaluation_to_dict_has_required_keys() -> None:
    ev = ContextEvaluation(
        task_summary="test",
        retrieved_atom_ids=["a"],
        used_atom_ids=["a"],
        ignored_atom_ids=[],
        context_status="sufficient",
        confidence=0.9,
        issues=[],
        required_actions=[],
        final_action="answer",
    )
    d = ev.to_dict()
    required = {
        "task_summary", "retrieved_atom_ids", "used_atom_ids",
        "ignored_atom_ids", "context_status", "confidence",
        "issues", "required_actions", "final_action", "trace_id",
    }
    missing = required - set(d.keys())
    assert not missing, f"to_dict() missing: {missing}"
    _pass_test("ContextEvaluation.to_dict() has all required keys")


def test_allowed_sets_complete() -> None:
    assert "sufficient" in ALLOWED_STATUSES
    assert "unsafe_or_blocked" in ALLOWED_STATUSES
    assert "answer" in ALLOWED_ACTIONS
    assert "refuse" in ALLOWED_ACTIONS
    _pass_test("ALLOWED_STATUSES and ALLOWED_ACTIONS are populated correctly")


# ── Integration tests (real DB, StubLLM) ─────────────────────────────────────

def test_evaluator_active_atoms_used() -> None:
    """Active, high-confidence atoms are used in the evaluation."""
    atom_id = "00000000-0000-0000-0000-111111111111"
    atoms = [_atom(atom_id=atom_id, lifecycle="active", confidence=0.9, similarity=0.85)]
    llm = _StubLLM([_critic_json("sufficient", 0.9, [atom_id])])
    evaluator = RuntimeContextEvaluator(llm=llm)
    result = evaluator.evaluate("What is the project preference?", atoms, use_critic=True)
    assert atom_id in result.used_atom_ids, f"expected atom in used_atom_ids: {result.used_atom_ids}"
    _pass_test("evaluator: active atom included in used_atom_ids")


def test_evaluator_superseded_atom_not_used() -> None:
    """Superseded atoms are excluded before the critic even sees them."""
    atom_id = "00000000-0000-0000-0000-222222222222"
    atoms = [_atom(atom_id=atom_id, lifecycle="superseded", similarity=0.95)]
    llm = _StubLLM([])  # no LLM call expected (nothing passes pre-filter)
    evaluator = RuntimeContextEvaluator(llm=llm)
    result = evaluator.evaluate("Task question", atoms, use_critic=False)
    assert atom_id not in result.used_atom_ids
    ignored_reasons = [i["reason"] for i in result.ignored_atom_ids]
    assert "lifecycle_superseded" in ignored_reasons
    _pass_test("evaluator: superseded atom excluded (lifecycle_superseded)")


def test_evaluator_archived_atom_not_used() -> None:
    atom_id = "00000000-0000-0000-0000-333333333333"
    atoms = [_atom(atom_id=atom_id, lifecycle="archived", similarity=0.95)]
    evaluator = RuntimeContextEvaluator(llm=_StubLLM([]))
    result = evaluator.evaluate("Task", atoms, use_critic=False)
    assert atom_id not in result.used_atom_ids
    _pass_test("evaluator: archived atom excluded")


def test_evaluator_conflicting_atoms_produce_caveat() -> None:
    """Two conflicting atoms → low confidence → answer_with_caveat."""
    a1 = _atom("00000000-0000-0000-0000-aaaaaaaaaaaa", disagreement=0.75)
    a2 = _atom("00000000-0000-0000-0000-bbbbbbbbbbbb", lifecycle="contested", similarity=0.82)
    llm = _StubLLM([
        _critic_json("conflicting", 0.65, [a1["id"], a2["id"]])
    ])
    evaluator = RuntimeContextEvaluator(llm=llm)
    result = evaluator.evaluate("Task with conflicting context", [a1, a2])
    assert result.final_action in ("answer_with_caveat", "verify_then_answer"), (
        f"expected caveat action, got {result.final_action}"
    )
    _pass_test("evaluator: conflicting atoms produce caveat action", f"action={result.final_action}")


def test_evaluator_no_relevant_atoms_produces_insufficient() -> None:
    """All atoms fail the relevance threshold → insufficient context."""
    atoms = [_atom(similarity=0.10), _atom(atom_id="00000000-0000-0000-0000-cccccccccccc", similarity=0.15)]
    evaluator = RuntimeContextEvaluator(llm=_StubLLM([]))
    result = evaluator.evaluate("Any task", atoms, use_critic=False)
    assert result.context_status == "insufficient"
    assert result.final_action == "answer_with_caveat"
    _pass_test("evaluator: all atoms below relevance threshold → insufficient")


def test_evaluator_low_confidence_critical_lowers_confidence() -> None:
    """Low-confidence atoms reduce aggregate confidence below 0.5."""
    atoms = [
        _atom(atom_id="00000000-0000-0000-0000-dddddddddddd", confidence=0.25, similarity=0.80),
        _atom(atom_id="00000000-0000-0000-0000-eeeeeeeeeeee", confidence=0.30, similarity=0.78),
    ]
    llm = _StubLLM([_critic_json("unsupported", 0.35,
                                  ["00000000-0000-0000-0000-dddddddddddd",
                                   "00000000-0000-0000-0000-eeeeeeeeeeee"])])
    evaluator = RuntimeContextEvaluator(llm=llm)
    result = evaluator.evaluate("Important task", atoms)
    assert result.confidence < 0.55, f"expected low confidence, got {result.confidence}"
    assert result.final_action in ("answer_with_caveat", "verify_then_answer")
    _pass_test("evaluator: low-confidence atoms reduce final confidence", f"conf={result.confidence}")


def test_evaluator_irrelevant_atoms_ignored_by_critic() -> None:
    """Critic marks atoms as irrelevant even if they passed the pre-filter."""
    a_good = _atom("00000000-0000-0000-0000-fffffffffffg", similarity=0.90)
    a_irrelevant = _atom("00000000-0000-0000-0000-ffffffffffff", similarity=0.75)
    llm = _StubLLM([
        _critic_json(
            "sufficient", 0.88,
            used_ids=[a_good["id"]],
            ignored=[{"atom_id": a_irrelevant["id"], "reason": "irrelevant_to_current_task"}],
        )
    ])
    evaluator = RuntimeContextEvaluator(llm=llm)
    result = evaluator.evaluate("Specific task", [a_good, a_irrelevant], use_critic=True)
    assert a_irrelevant["id"] not in result.used_atom_ids
    irrelevant_ignored = any(
        i["atom_id"] == a_irrelevant["id"] for i in result.ignored_atom_ids
    )
    assert irrelevant_ignored, "irrelevant atom should appear in ignored_atom_ids"
    _pass_test("evaluator: critic-marked irrelevant atom excluded from used_atom_ids")


def test_evaluator_always_returns_evaluation() -> None:
    """evaluate() never raises; always returns a ContextEvaluation."""
    evaluator = RuntimeContextEvaluator(llm=_StubLLM([]))
    result = evaluator.evaluate("anything", [], use_critic=False)
    assert isinstance(result, ContextEvaluation)
    assert result.context_status in ALLOWED_STATUSES
    assert result.final_action in ALLOWED_ACTIONS
    _pass_test("evaluator: always returns ContextEvaluation with valid fields")


def test_evaluator_trace_stored_in_db() -> None:
    """Storing a context trace writes a row to runtime_context_traces."""
    from app.memory_store import MemoryStore
    store = MemoryStore(ollama_client=_StubLLM([]))
    ev = ContextEvaluation(
        task_summary="trace-storage-test",
        retrieved_atom_ids=["00000000-0000-0000-0000-000000000001"],
        used_atom_ids=["00000000-0000-0000-0000-000000000001"],
        ignored_atom_ids=[],
        context_status="sufficient",
        confidence=0.9,
        issues=[],
        required_actions=[],
        final_action="answer",
    )
    trace_id = None
    try:
        trace_id = store.store_context_trace(ev.to_dict())
        assert trace_id, "trace_id should be set"
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT final_action, context_status FROM runtime_context_traces WHERE id = %s::uuid;",
                    (trace_id,),
                )
                row = cur.fetchone()
        assert row, "trace row not found in DB"
        assert row[0] == "answer"
        assert row[1] == "sufficient"
        _pass_test("evaluator: context trace stored in DB", f"trace={trace_id[:8]}")
    except Exception as exc:
        _fail_test("evaluator: context trace stored in DB", str(exc))
        traceback.print_exc()
    finally:
        _cleanup_trace(trace_id)


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    _p("\n=== test_context_evaluator ===\n")
    _p("-- Unit tests: deterministic pre-filter --")
    test_prefilter_active_atom_passes()
    test_prefilter_superseded_atom_ignored()
    test_prefilter_deprecated_atom_ignored()
    test_prefilter_archived_atom_ignored()
    test_prefilter_low_similarity_ignored()
    test_prefilter_scope_mismatch_ignored()
    test_prefilter_global_scope_not_ignored()
    test_prefilter_contested_flagged_as_issue()
    test_prefilter_high_disagreement_flagged()
    test_prefilter_low_confidence_flagged()
    test_prefilter_stale_atom_flagged()

    _p("\n-- Unit tests: risk gate --")
    test_risk_gate_no_usable_atoms_insufficient()
    test_risk_gate_unsafe_forces_refuse()
    test_risk_gate_low_confidence_upgrades_to_caveat()
    test_risk_gate_conflicting_issues_lower_confidence()
    test_risk_gate_many_conflicts_change_status()
    test_risk_gate_verify_then_answer_passes_through()

    _p("\n-- Unit tests: critic parser --")
    test_parse_critic_valid_json()
    test_parse_critic_unknown_status_normalised()
    test_parse_critic_invalid_ids_filtered()
    test_parse_critic_invalid_json_fallback()

    _p("\n-- Unit tests: dataclass --")
    test_evaluation_to_dict_has_required_keys()
    test_allowed_sets_complete()

    _p("\n-- Integration tests (real DB, StubLLM) --")
    test_evaluator_active_atoms_used()
    test_evaluator_superseded_atom_not_used()
    test_evaluator_archived_atom_not_used()
    test_evaluator_conflicting_atoms_produce_caveat()
    test_evaluator_no_relevant_atoms_produces_insufficient()
    test_evaluator_low_confidence_critical_lowers_confidence()
    test_evaluator_irrelevant_atoms_ignored_by_critic()
    test_evaluator_always_returns_evaluation()
    test_evaluator_trace_stored_in_db()

    _p(f"\nResults: {_pass} PASS  {_warn} WARN  {_fail} FAIL\n")
    if _fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
