"""Tests for the Memory Commit Pipeline.

Uses a StubLLM to inject deterministic reconciler and critic responses without
requiring a live Ollama instance. Writes to the real DB and cleans up in
finally blocks.

Run with: python scripts/test_commit_pipeline.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import traceback
from typing import Any

import psycopg

# ── Ensure project root is on path ───────────────────────────────────────────
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_config
from app.commit_pipeline import (
    MemoryCommitPipeline,
    _apply_risk_gate,
    _parse_critic_response,
    ALLOWED_DECISIONS,
)


# ── Stub LLM ──────────────────────────────────────────────────────────────────

class _StubLLM:
    """Deterministic LLM stub for pipeline tests.

    Each call to generate_response() pops and returns the next response from
    the queue. If the queue is empty, falls back to a safe default JSON.

    embed_text() returns a reproducible 4096-dim vector derived from the
    content hash, ensuring near-duplicate detection behaves predictably.
    """

    _DIM = 4096

    def __init__(self, responses: list[str]) -> None:
        self._queue: list[str] = list(responses)

    def generate_response(self, prompt: str, system: str | None = None) -> str:
        if self._queue:
            return self._queue.pop(0)
        return json.dumps({
            "relationship": "new",
            "matched_memory_ids": [],
            "reason": "stub default",
            "recommended_action": "store_new",
        })

    def embed_text(self, text: str) -> list[float]:
        h = int(hashlib.md5(text.encode()).hexdigest(), 16)
        v = [0.0] * self._DIM
        # Spread a few non-zero values around so similarity works
        for i in range(8):
            idx = (h + i * 491) % self._DIM
            v[idx] = float((h >> (i * 4)) & 0xFF) / 255.0
        return v


def _reconciler_json(
    relationship: str = "new",
    matched_ids: list[str] | None = None,
    reason: str = "stub",
    recommended_action: str = "store_new",
    related_memories: list[dict] | None = None,
) -> str:
    return json.dumps({
        "relationship": relationship,
        "matched_memory_ids": matched_ids or [],
        "reason": reason,
        "recommended_action": recommended_action,
        "related_memories": related_memories or [],
    })


def _critic_json(
    decision: str = "commit",
    final_text: str = "Test memory statement.",
    memory_type: str = "instruction",
    scope: str | None = "test",
    confidence: float = 0.9,
    notes: list[str] | None = None,
    rejection_reason: str | None = None,
) -> str:
    return json.dumps({
        "decision": decision,
        "final_memory_text": final_text,
        "memory_type": memory_type,
        "scope": scope,
        "confidence": confidence,
        "critic_notes": notes or ["Durable and clear."],
        "rejection_reason": rejection_reason,
    })


# ── Test helpers ──────────────────────────────────────────────────────────────

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


def _cleanup_atom(atom_id: str | None) -> None:
    if not atom_id:
        return
    try:
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_atoms WHERE id = %s::uuid;", (atom_id,)
                )
            conn.commit()
    except Exception:
        pass


def _cleanup_trace(trace_id: str | None) -> None:
    if not trace_id:
        return
    try:
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_commit_traces WHERE id = %s::uuid;",
                    (trace_id,),
                )
            conn.commit()
    except Exception:
        pass


def _cleanup_proposal(proposal_id: str | None) -> None:
    if not proposal_id:
        return
    try:
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memory_proposals WHERE id = %s::uuid;",
                    (proposal_id,),
                )
            conn.commit()
    except Exception:
        pass


# ── Unit tests (no DB) ────────────────────────────────────────────────────────

def test_risk_gate_duplicate_always_reinforces() -> None:
    decision, action = _apply_risk_gate("duplicate", ["abc"], "commit")
    assert decision == "reinforce_existing", f"expected reinforce_existing, got {decision}"
    assert action == "signal_added"
    _pass_test("risk_gate: duplicate → reinforce_existing (critic=commit ignored)")


def test_risk_gate_reinforcement_always_reinforces() -> None:
    decision, action = _apply_risk_gate("reinforcement", ["xyz"], "commit")
    assert decision == "reinforce_existing", f"got {decision}"
    _pass_test("risk_gate: reinforcement → reinforce_existing (critic overridden)")


def test_risk_gate_critic_reject_honoured_for_new() -> None:
    decision, action = _apply_risk_gate("new", [], "reject")
    assert decision == "reject", f"got {decision}"
    _pass_test("risk_gate: new + critic=reject → reject")


def test_risk_gate_conflict_defaults_to_mark_conflict() -> None:
    decision, _ = _apply_risk_gate("conflict", ["id1"], "propose_for_review")
    assert decision == "mark_conflict", f"got {decision}"
    _pass_test("risk_gate: conflict + critic=propose → mark_conflict")


def test_risk_gate_conflict_critic_supersede() -> None:
    decision, action = _apply_risk_gate("conflict", ["id1"], "supersede_existing")
    assert decision == "supersede_existing", f"got {decision}"
    assert action == "superseded"
    _pass_test("risk_gate: conflict + critic=supersede_existing → supersede_existing")


def test_risk_gate_refinement_commit_becomes_refine() -> None:
    decision, action = _apply_risk_gate("refinement", ["id2"], "commit")
    assert decision == "refine_existing", f"got {decision}"
    assert action == "superseded"
    _pass_test("risk_gate: refinement + critic=commit → refine_existing")


def test_risk_gate_new_commit() -> None:
    decision, action = _apply_risk_gate("new", [], "commit")
    assert decision == "commit", f"got {decision}"
    assert action == "created"
    _pass_test("risk_gate: new + critic=commit → commit")


def test_risk_gate_new_refine_with_matched_ids() -> None:
    decision, action = _apply_risk_gate("new", ["old-id"], "refine_existing")
    assert decision == "refine_existing", f"got {decision}"
    _pass_test("risk_gate: new + critic=refine + matched_ids → refine_existing")


def test_risk_gate_new_refine_without_matched_ids() -> None:
    decision, action = _apply_risk_gate("new", [], "refine_existing")
    assert decision == "commit", f"got {decision}"
    _pass_test("risk_gate: new + critic=refine + no matched_ids → commit")


def test_parse_critic_response_valid_json() -> None:
    raw = _critic_json(decision="commit", final_text="Use web search for current data.", scope="global")
    parsed = _parse_critic_response(raw, "fallback", "fact", None, 0.5)
    assert parsed["decision"] == "commit"
    assert parsed["final_memory_text"] == "Use web search for current data."
    assert parsed["scope"] == "global"
    _pass_test("parse_critic_response: valid JSON parsed correctly")


def test_parse_critic_response_invalid_json() -> None:
    parsed = _parse_critic_response("not json at all", "fallback", "fact", None, 0.7)
    assert parsed["decision"] == "propose_for_review"
    assert parsed["final_memory_text"] == "fallback"
    _pass_test("parse_critic_response: invalid JSON falls back to propose_for_review")


def test_parse_critic_response_unknown_decision_normalised() -> None:
    raw = json.dumps({
        "decision": "completely_unknown",
        "final_memory_text": "x",
        "memory_type": "fact",
        "scope": None,
        "confidence": 0.8,
        "critic_notes": [],
        "rejection_reason": None,
    })
    parsed = _parse_critic_response(raw, "fallback", "fact", None, 0.8)
    assert parsed["decision"] == "propose_for_review"
    _pass_test("parse_critic_response: unknown decision normalised to propose_for_review")


def test_all_allowed_decisions_valid() -> None:
    expected = {
        "commit", "refine_existing", "supersede_existing",
        "reinforce_existing", "mark_conflict", "propose_for_review", "reject",
    }
    assert ALLOWED_DECISIONS == expected, f"ALLOWED_DECISIONS mismatch: {ALLOWED_DECISIONS}"
    _pass_test("ALLOWED_DECISIONS set is complete")


# ── Integration tests (real DB) ───────────────────────────────────────────────

def test_pipeline_new_candidate_commits() -> None:
    """A fresh durable preference → commit decision, atom created in DB."""
    unique = "pipeline-test-commit-" + hashlib.md5(b"commit-test-1").hexdigest()[:8]
    candidate = {
        "content": f"Always run tests before merging code. ({unique})",
        "memory_type": "instruction",
        "scope": "pipeline-test",
        "confidence": 0.9,
        "importance": 0.7,
    }
    llm = _StubLLM([
        _reconciler_json("new"),
        _critic_json(
            "commit",
            f"Always run tests before merging code. ({unique})",
            scope="pipeline-test",
        ),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        assert decision.decision == "commit", f"expected commit, got {decision.decision}"
        assert decision.committed_atom_id, "no committed_atom_id"
        assert decision.trace_id, "no trace_id"
        _pass_test("pipeline: new candidate → commit", f"atom={decision.committed_atom_id[:8]}")
    except Exception as exc:
        _fail_test("pipeline: new candidate → commit", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_atom(decision.committed_atom_id)
            _cleanup_trace(decision.trace_id)


def test_pipeline_reinforcement_adds_signal() -> None:
    """Reconciler returns reinforcement → reinforce_existing (no new atom)."""
    candidate = {
        "content": "Prefer explicit variable names over single-letter names.",
        "memory_type": "instruction",
        "scope": "pipeline-test",
        "confidence": 0.85,
        "importance": 0.6,
    }
    llm = _StubLLM([
        _reconciler_json("reinforcement", matched_ids=["00000000-0000-0000-0000-000000000001"]),
        _critic_json("commit", "Prefer explicit variable names over single-letter names.", scope="pipeline-test"),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        # Risk gate must override critic=commit when reconciler=reinforcement
        assert decision.decision == "reinforce_existing", (
            f"expected reinforce_existing, got {decision.decision}"
        )
        assert decision.committed_atom_id is None, "should not create new atom"
        _pass_test("pipeline: reinforcement → reinforce_existing (critic overridden)")
    except Exception as exc:
        _fail_test("pipeline: reinforcement → reinforce_existing", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_trace(decision.trace_id)


def test_pipeline_vague_candidate_rejected() -> None:
    """Critic classifies candidate as too_vague → reject decision."""
    candidate = {
        "content": "Do things better.",
        "memory_type": "fact",
        "scope": None,
        "confidence": 0.5,
    }
    llm = _StubLLM([
        _reconciler_json("new"),
        _critic_json("reject", "Do things better.", notes=["Too vague to be actionable."], rejection_reason="too_vague"),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        assert decision.decision == "reject", f"expected reject, got {decision.decision}"
        assert decision.committed_atom_id is None
        assert decision.rejection_reason == "too_vague"
        _pass_test("pipeline: vague candidate → reject", f"reason={decision.rejection_reason}")
    except Exception as exc:
        _fail_test("pipeline: vague candidate → reject", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_trace(decision.trace_id)


def test_pipeline_sensitive_content_rejected() -> None:
    """Critic classifies candidate as sensitive → reject decision."""
    candidate = {
        "content": "My API key is sk-test-1234567890abcdef.",
        "memory_type": "fact",
        "scope": None,
        "confidence": 0.9,
    }
    llm = _StubLLM([
        _reconciler_json("new"),
        _critic_json("reject", None, notes=["Contains sensitive credential."], rejection_reason="sensitive"),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        assert decision.decision == "reject", f"expected reject, got {decision.decision}"
        assert decision.rejection_reason == "sensitive"
        assert decision.final_memory_text is None
        _pass_test("pipeline: sensitive candidate → reject", "rejection_reason=sensitive")
    except Exception as exc:
        _fail_test("pipeline: sensitive candidate → reject", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_trace(decision.trace_id)


def test_pipeline_conflict_queued_for_proposal() -> None:
    """Reconciler returns conflict + critic says mark_conflict → propose."""
    candidate = {
        "content": "Use tabs for Python indentation.",
        "memory_type": "instruction",
        "scope": "pipeline-test",
        "confidence": 0.75,
    }
    llm = _StubLLM([
        _reconciler_json("conflict", matched_ids=["00000000-0000-0000-0000-000000000002"]),
        _critic_json("mark_conflict", "Use tabs for Python indentation.", scope="pipeline-test"),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        assert decision.decision == "mark_conflict", f"expected mark_conflict, got {decision.decision}"
        assert decision.committed_atom_id is None
        _pass_test("pipeline: conflict → mark_conflict / queued for proposal")
    except Exception as exc:
        _fail_test("pipeline: conflict → mark_conflict", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_proposal(decision.proposal_id)
            _cleanup_trace(decision.trace_id)


def test_pipeline_refinement_supersedes_atom() -> None:
    """Reconciler=refinement + critic=refine_existing → old atom superseded."""
    unique = hashlib.md5(b"refine-test").hexdigest()[:8]

    # First create an atom to supersede
    from app.memory_store import MemoryStore
    from app.llm_provider import get_llm_client

    # Use real LLM only for the initial store (minimal embedding needed)
    original_content = f"Use spaces for indentation. (refine-test-{unique})"
    store = MemoryStore()
    old_atom_id: str | None = None
    decision = None

    try:
        # Create the atom we want to supersede using the real embed path
        real_llm = get_llm_client()
        old_atom_id, _ = store.store_memory_with_signal(
            content=original_content,
            context_summary=original_content,
            memory_type="instruction",
            scope="pipeline-test",
            confidence=0.8,
            importance=0.5,
            relationship="new",
            reconciliation_reason="test setup",
            raw_input=original_content,
            signal_metadata=None,
            source_key="test",
            source_type="test",
            task_run_id=None,
        )
    except Exception as exc:
        _warn_test(
            "pipeline: refinement supersedes atom",
            f"Could not create fixture atom (real LLM unavailable?): {exc}",
        )
        return

    try:
        refined_content = f"Always use 4-space indentation in Python. (refine-test-{unique})"
        candidate = {
            "content": refined_content,
            "memory_type": "instruction",
            "scope": "pipeline-test",
            "confidence": 0.92,
        }
        llm = _StubLLM([
            _reconciler_json("refinement", matched_ids=[old_atom_id]),
            _critic_json("refine_existing", refined_content, scope="pipeline-test"),
        ])
        pipeline = MemoryCommitPipeline(llm=llm)
        decision = pipeline.commit_candidate(candidate)

        assert decision.decision == "refine_existing", (
            f"expected refine_existing, got {decision.decision}"
        )
        assert decision.committed_atom_id, "no committed_atom_id"
        assert old_atom_id in decision.supersedes_atom_ids, (
            f"old atom not in supersedes_atom_ids: {decision.supersedes_atom_ids}"
        )

        # Verify lifecycle_status was updated in DB
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lifecycle_status FROM memory_atoms WHERE id = %s::uuid;",
                    (old_atom_id,),
                )
                row = cur.fetchone()
        assert row and row[0] == "superseded", (
            f"old atom lifecycle_status should be superseded, got {row[0] if row else None}"
        )
        _pass_test(
            "pipeline: refinement supersedes old atom",
            f"new={decision.committed_atom_id[:8]} superseded={old_atom_id[:8]}",
        )
    except Exception as exc:
        _fail_test("pipeline: refinement supersedes atom", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_atom(decision.committed_atom_id)
            _cleanup_trace(decision.trace_id)
        _cleanup_atom(old_atom_id)


def test_pipeline_commit_trace_stored() -> None:
    """Every pipeline run stores a row in memory_commit_traces."""
    unique = hashlib.md5(b"trace-test").hexdigest()[:8]
    candidate = {
        "content": f"Document all public functions. (trace-test-{unique})",
        "memory_type": "instruction",
        "scope": "pipeline-test",
        "confidence": 0.88,
    }
    llm = _StubLLM([
        _reconciler_json("new"),
        _critic_json("commit", f"Document all public functions. (trace-test-{unique})", scope="pipeline-test"),
    ])
    pipeline = MemoryCommitPipeline(llm=llm)
    decision = None
    try:
        decision = pipeline.commit_candidate(candidate)
        assert decision.trace_id, "trace_id should be set"

        # Confirm row exists
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT decision, candidate_text FROM memory_commit_traces WHERE id = %s::uuid;",
                    (decision.trace_id,),
                )
                row = cur.fetchone()
        assert row, "trace row not found in DB"
        assert row[0] == decision.decision
        _pass_test("pipeline: commit trace stored in DB", f"trace={decision.trace_id[:8]}")
    except Exception as exc:
        _fail_test("pipeline: commit trace stored in DB", str(exc))
        traceback.print_exc()
    finally:
        if decision:
            _cleanup_atom(decision.committed_atom_id)
            _cleanup_trace(decision.trace_id)


def test_pipeline_empty_content_raises() -> None:
    """commit_candidate raises ValueError for empty content."""
    pipeline = MemoryCommitPipeline(llm=_StubLLM([]))
    try:
        pipeline.commit_candidate({"content": "  ", "memory_type": "fact"})
        _fail_test("pipeline: empty content raises ValueError", "expected ValueError, got none")
    except ValueError:
        _pass_test("pipeline: empty content raises ValueError")
    except Exception as exc:
        _fail_test("pipeline: empty content raises ValueError", f"wrong exception: {exc}")


def test_pipeline_to_dict_has_expected_keys() -> None:
    """CommitDecision.to_dict() contains all expected keys."""
    from app.commit_pipeline import CommitDecision
    d = CommitDecision(
        decision="commit",
        candidate_text="x",
        final_memory_text="y",
        scope="s",
        memory_type="fact",
        confidence=0.9,
        lifecycle_action="created",
    )
    result = d.to_dict()
    required = {
        "decision", "candidate_text", "final_memory_text", "scope", "memory_type",
        "confidence", "lifecycle_action", "duplicate_atom_ids", "reinforces_atom_ids",
        "refines_atom_ids", "supersedes_atom_ids", "conflicts_with_atom_ids",
        "critic_notes", "write_action", "committed_atom_id", "committed_signal_id",
        "proposal_id", "rejection_reason", "trace_id",
    }
    missing = required - set(result.keys())
    assert not missing, f"to_dict() missing keys: {missing}"
    _pass_test("CommitDecision.to_dict() has all expected keys")


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    _p("\n=== test_commit_pipeline ===\n")
    _p("-- Unit tests (no DB required) --")

    # Risk gate unit tests
    test_risk_gate_duplicate_always_reinforces()
    test_risk_gate_reinforcement_always_reinforces()
    test_risk_gate_critic_reject_honoured_for_new()
    test_risk_gate_conflict_defaults_to_mark_conflict()
    test_risk_gate_conflict_critic_supersede()
    test_risk_gate_refinement_commit_becomes_refine()
    test_risk_gate_new_commit()
    test_risk_gate_new_refine_with_matched_ids()
    test_risk_gate_new_refine_without_matched_ids()

    # Critic parser unit tests
    test_parse_critic_response_valid_json()
    test_parse_critic_response_invalid_json()
    test_parse_critic_response_unknown_decision_normalised()
    test_all_allowed_decisions_valid()
    test_pipeline_to_dict_has_expected_keys()

    _p("\n-- Integration tests (real DB) --")
    test_pipeline_new_candidate_commits()
    test_pipeline_reinforcement_adds_signal()
    test_pipeline_vague_candidate_rejected()
    test_pipeline_sensitive_content_rejected()
    test_pipeline_conflict_queued_for_proposal()
    test_pipeline_refinement_supersedes_atom()
    test_pipeline_commit_trace_stored()
    test_pipeline_empty_content_raises()

    _p(f"\nResults: {_pass} PASS  {_warn} WARN  {_fail} FAIL\n")
    if _fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
