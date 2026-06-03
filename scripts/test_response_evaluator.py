"""Tests for the Response Draft Evaluator.

Covers deterministic logic (hard overrides, parse fallback, verdict derivation)
and integration paths (stub LLM, DB trace storage).

Run with: python scripts/test_response_evaluator.py
"""
from __future__ import annotations

import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import psycopg

from app.config import get_config
from app.response_evaluator import (
    RESPONSE_VERDICTS,
    OVERSTATEMENT_LEVELS,
    ResponseEvaluation,
    ResponseEvaluator,
    _fallback,
    _hard_override,
    _parse_critic_response,
    _deterministic_verdict,
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
    _p(f"  PASS  {name}" + (f" — {detail}" if detail else ""))


def _fail_test(name: str, detail: str = "") -> None:
    global _fail
    _fail += 1
    _p(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))


def _cleanup_trace(trace_id: str | None) -> None:
    if not trace_id:
        return
    try:
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM runtime_response_traces WHERE id = %s::uuid;",
                    (trace_id,),
                )
            conn.commit()
    except Exception:
        pass


# ── Stub LLM + ContextEval ────────────────────────────────────────────────────

class _StubLLM:
    def __init__(self, responses: list[str]) -> None:
        self._queue = list(responses)

    def generate_response(self, prompt: str, system: str | None = None) -> str:
        return self._queue.pop(0) if self._queue else json.dumps({
            "verdict": "approved",
            "action_followed": True,
            "overstatement_risk": "none",
            "issues": [],
            "revised_answer": None,
            "commit_candidates": [],
            "reasoning": "stub: approved",
        })

    def embed_text(self, text: str) -> list[float]:
        return [0.1] * 4096


def _critic_json(
    verdict: str = "approved",
    action_followed: bool = True,
    overstatement: str = "none",
    issues: list[dict] | None = None,
    revised: str | None = None,
    candidates: list[str] | None = None,
    reasoning: str = "stub verdict",
) -> str:
    return json.dumps({
        "verdict": verdict,
        "action_followed": action_followed,
        "overstatement_risk": overstatement,
        "issues": issues or [],
        "revised_answer": revised,
        "commit_candidates": candidates or [],
        "reasoning": reasoning,
    })


class _StubContextEval:
    def __init__(
        self,
        final_action: str = "answer",
        context_status: str = "sufficient",
        confidence: float = 0.85,
        issues: list[dict] | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.final_action = final_action
        self.context_status = context_status
        self.confidence = confidence
        self.issues = issues or []
        self.trace_id = trace_id

    def to_dict(self) -> dict:
        return {
            "context_status": self.context_status,
            "confidence": self.confidence,
            "final_action": self.final_action,
            "issues": self.issues,
        }


# ── Unit tests: hard override ─────────────────────────────────────────────────

def test_hard_override_refuse_returns_blocked() -> None:
    result = _hard_override("Here is a detailed answer about X.", "refuse")
    assert result == "blocked"
    _pass_test("hard_override: refuse action → blocked")


def test_hard_override_ask_user_no_question_returns_needs_revision() -> None:
    result = _hard_override("The answer is 42.", "ask_user")
    assert result == "needs_revision"
    _pass_test("hard_override: ask_user + no question mark → needs_revision")


def test_hard_override_ask_user_with_question_returns_none() -> None:
    result = _hard_override("Could you clarify what you mean?", "ask_user")
    assert result is None
    _pass_test("hard_override: ask_user + question mark → None (no override)")


def test_hard_override_normal_answer_returns_none() -> None:
    result = _hard_override("The capital of France is Paris.", "answer")
    assert result is None
    _pass_test("hard_override: normal answer action → None")


def test_hard_override_answer_with_caveat_returns_none() -> None:
    result = _hard_override("Best guess: it might be around 50.", "answer_with_caveat")
    assert result is None
    _pass_test("hard_override: answer_with_caveat → None (no override)")


# ── Unit tests: critic parser ─────────────────────────────────────────────────

def test_parse_critic_valid_json() -> None:
    raw = _critic_json("approved", True, "none", reasoning="looks good")
    result = _parse_critic_response(raw, "draft")
    assert result["verdict"] == "approved"
    assert result["action_followed"] is True
    assert result["overstatement_risk"] == "none"
    _pass_test("parse_critic_response: valid JSON parsed correctly")


def test_parse_critic_unknown_verdict_normalised() -> None:
    raw = json.dumps({
        "verdict": "totally_made_up",
        "action_followed": True,
        "overstatement_risk": "low",
        "issues": [],
        "revised_answer": None,
        "commit_candidates": [],
        "reasoning": "test",
    })
    result = _parse_critic_response(raw, "draft")
    assert result["verdict"] == "needs_caveat", f"got {result['verdict']}"
    _pass_test("parse_critic_response: unknown verdict normalised to needs_caveat")


def test_parse_critic_unknown_overstatement_normalised() -> None:
    raw = json.dumps({
        "verdict": "approved",
        "action_followed": True,
        "overstatement_risk": "extreme",
        "issues": [],
        "revised_answer": None,
        "commit_candidates": [],
        "reasoning": "test",
    })
    result = _parse_critic_response(raw, "draft")
    assert result["overstatement_risk"] == "low"
    _pass_test("parse_critic_response: unknown overstatement level normalised to low")


def test_parse_critic_invalid_json_returns_fallback() -> None:
    result = _parse_critic_response("not json at all!!!", "draft")
    assert result["verdict"] == "needs_caveat"
    assert any(i["type"] == "critic_parse_error" for i in result["issues"])
    _pass_test("parse_critic_response: invalid JSON → fallback with critic_parse_error issue")


def test_parse_critic_strips_markdown_fences() -> None:
    raw = "```json\n" + _critic_json("approved") + "\n```"
    result = _parse_critic_response(raw, "draft")
    assert result["verdict"] == "approved"
    _pass_test("parse_critic_response: markdown fences stripped correctly")


def test_parse_critic_commit_candidates_extracted() -> None:
    raw = _critic_json(candidates=["Lesson A is important.", "Lesson B matters."])
    result = _parse_critic_response(raw, "draft")
    assert len(result["commit_candidates"]) == 2
    _pass_test("parse_critic_response: commit_candidates extracted")


def test_parse_critic_short_candidates_filtered() -> None:
    raw = _critic_json(candidates=["ok", "this is a valid lesson sentence here."])
    result = _parse_critic_response(raw, "draft")
    # "ok" is < 10 chars and should be filtered
    assert not any(c == "ok" for c in result["commit_candidates"])
    _pass_test("parse_critic_response: short commit candidates filtered out")


# ── Unit tests: deterministic verdict ────────────────────────────────────────

def test_deterministic_verdict_sufficient_approved() -> None:
    ce = {"context_status": "sufficient", "confidence": 0.9, "final_action": "answer", "issues": []}
    result = _deterministic_verdict("The answer is clear.", "answer", ce)
    assert result["verdict"] == "approved"
    _pass_test("deterministic_verdict: sufficient context → approved")


def test_deterministic_verdict_conflicting_needs_caveat() -> None:
    ce = {"context_status": "conflicting", "confidence": 0.5, "final_action": "answer_with_caveat", "issues": []}
    result = _deterministic_verdict("The answer is X.", "answer_with_caveat", ce)
    assert result["verdict"] == "needs_caveat"
    _pass_test("deterministic_verdict: conflicting context → needs_caveat")


def test_deterministic_verdict_low_confidence_needs_caveat() -> None:
    ce = {"context_status": "sufficient", "confidence": 0.35, "final_action": "answer", "issues": []}
    result = _deterministic_verdict("The answer is X.", "answer", ce)
    assert result["verdict"] == "needs_caveat"
    _pass_test("deterministic_verdict: low confidence → needs_caveat")


def test_deterministic_verdict_ask_user_no_question() -> None:
    ce = {"context_status": "needs_user_clarification", "confidence": 0.5, "final_action": "ask_user", "issues": []}
    result = _deterministic_verdict("I think the answer is 42.", "ask_user", ce)
    assert result["verdict"] == "needs_revision"
    _pass_test("deterministic_verdict: ask_user + no question → needs_revision")


# ── Unit tests: ResponseEvaluation dataclass ──────────────────────────────────

def test_response_evaluation_to_dict_has_required_keys() -> None:
    ev = ResponseEvaluation(
        verdict="approved",
        action_followed=True,
        overstatement_risk="none",
        issues=[],
        revised_answer=None,
        commit_candidates=[],
        reasoning="all good",
    )
    d = ev.to_dict()
    required = {
        "verdict", "action_followed", "overstatement_risk",
        "issues", "revised_answer", "commit_candidates", "reasoning", "trace_id",
    }
    missing = required - set(d.keys())
    assert not missing, f"to_dict() missing keys: {missing}"
    _pass_test("ResponseEvaluation.to_dict() has all required keys")


def test_allowed_sets_complete() -> None:
    assert "approved" in RESPONSE_VERDICTS
    assert "blocked" in RESPONSE_VERDICTS
    assert "none" in OVERSTATEMENT_LEVELS
    assert "high" in OVERSTATEMENT_LEVELS
    _pass_test("RESPONSE_VERDICTS and OVERSTATEMENT_LEVELS are populated correctly")


# ── Integration tests (StubLLM) ───────────────────────────────────────────────

def test_evaluator_approved_answer_passes_through() -> None:
    """Good draft with approved critic verdict → no revision."""
    llm = _StubLLM([_critic_json("approved", True, "none", reasoning="all good")])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("answer", "sufficient", 0.9)
    result = ev.evaluate("Python is a programming language.", ce, [], "What is Python?")
    assert result.verdict == "approved"
    assert result.revised_answer is None
    _pass_test("evaluator: approved draft passes through unchanged")


def test_evaluator_refuse_action_blocks_answer() -> None:
    """final_action=refuse triggers hard block regardless of critic."""
    llm = _StubLLM([])  # critic should NOT be called
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("refuse", "unsafe_or_blocked", 0.1)
    result = ev.evaluate(
        "Here is detailed info about X.",
        ce,
        [],
        "Tell me about X",
    )
    assert result.verdict == "blocked"
    assert result.action_followed is False
    _pass_test("evaluator: refuse action hard-blocks the draft")


def test_evaluator_refuse_generates_polite_refusal() -> None:
    """Hard block for refuse generates a revised refusal message."""
    refusal_text = "I'm unable to provide a confident answer on this topic right now."
    llm = _StubLLM([refusal_text])  # revision pass response
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("refuse", "unsafe_or_blocked", 0.1)
    result = ev.evaluate("Detailed answer here.", ce, [], "Bad question")
    assert result.revised_answer is not None
    _pass_test("evaluator: refuse generates revised refusal message")


def test_evaluator_ask_user_no_question_needs_revision() -> None:
    """final_action=ask_user but draft has no question → needs_revision."""
    llm = _StubLLM([
        _critic_json("approved"),            # critic (would say approved)
        "Could you clarify what you mean?",  # revision pass
    ])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("ask_user", "needs_user_clarification", 0.4)
    result = ev.evaluate(
        "The answer is 42.",
        ce,
        [],
        "Ambiguous question",
    )
    assert result.verdict == "needs_revision"
    _pass_test("evaluator: ask_user without question forces needs_revision")


def test_evaluator_verify_then_answer_runs_verification() -> None:
    """final_action=verify_then_answer triggers actual verification pass."""
    verified_text = "Best guess: Python was created around 1991 by Guido van Rossum."
    llm = _StubLLM([
        _critic_json("approved", True),  # critic says approved
        verified_text,                   # verification pass
    ])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("verify_then_answer", "needs_verification", 0.6)
    result = ev.evaluate(
        "Python was created in 1991.",
        ce,
        [],
        "When was Python created?",
    )
    # Verification pass should have run and produced a revised answer
    assert result.revised_answer is not None
    assert result.verdict == "needs_verification"
    _pass_test("evaluator: verify_then_answer triggers real verification pass",
               f"revised={result.revised_answer[:40]!r}")


def test_evaluator_needs_revision_critic_no_revised_runs_revision_pass() -> None:
    """Critic returns needs_revision without a revised_answer → revision pass triggered."""
    revised_text = "Weakly supported: Python may have been created in 1991."
    llm = _StubLLM([
        _critic_json("needs_revision", False, "medium",
                     issues=[{"type": "overstatement", "detail": "stated as fact", "severity": "medium"}]),
        revised_text,  # revision pass
    ])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("answer", "sufficient", 0.8)
    result = ev.evaluate(
        "Python was definitely created exactly in 1989.",
        ce,
        [],
        "When was Python created?",
    )
    assert result.verdict == "needs_revision"
    assert result.revised_answer is not None
    _pass_test("evaluator: needs_revision without revised_answer triggers revision pass")


def test_evaluator_needs_caveat_verdict_preserved() -> None:
    """Critic verdict of needs_caveat is preserved even if no revision is provided."""
    llm = _StubLLM([_critic_json("needs_caveat", False, "low", reasoning="missing labels")])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("answer_with_caveat", "stale", 0.55)
    result = ev.evaluate(
        "The config is at /etc/app.conf.",
        ce,
        [],
        "Where is the config?",
    )
    assert result.verdict == "needs_caveat"
    _pass_test("evaluator: needs_caveat verdict preserved")


def test_evaluator_commit_candidates_extracted() -> None:
    """Commit candidates from critic are surfaced in the evaluation."""
    candidates = ["The config file path is /etc/app.conf per project convention."]
    llm = _StubLLM([_critic_json("approved", True, "none", candidates=candidates)])
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("answer", "sufficient", 0.9)
    result = ev.evaluate("Config is at /etc/app.conf.", ce, [], "Config?")
    assert len(result.commit_candidates) == 1
    _pass_test("evaluator: commit candidates extracted correctly")


def test_evaluator_use_critic_false_deterministic() -> None:
    """use_critic=False skips LLM calls; verdict derived deterministically."""
    llm = _StubLLM([])  # no LLM calls expected
    ev = ResponseEvaluator(llm=llm)
    ce = _StubContextEval("answer", "sufficient", 0.9)
    result = ev.evaluate(
        "Clear answer.",
        ce,
        [],
        "Simple question?",
        use_critic=False,
    )
    assert result.verdict in RESPONSE_VERDICTS
    _pass_test("evaluator: use_critic=False produces valid verdict without LLM")


def test_evaluator_response_trace_stored_in_db() -> None:
    """store_response_trace writes a row to runtime_response_traces."""
    from app.memory_store import MemoryStore
    store = MemoryStore(ollama_client=_StubLLM([]))
    ev = ResponseEvaluation(
        verdict="approved",
        action_followed=True,
        overstatement_risk="none",
        issues=[],
        revised_answer=None,
        commit_candidates=["Test lesson for response trace."],
        reasoning="trace-storage-test",
    )
    trace_id = None
    try:
        trace_data = ev.to_dict()
        trace_data["user_message"] = "trace-storage-test"
        trace_data["draft_answer"] = "draft"
        trace_data["final_answer"] = "final"
        trace_data["context_trace_id"] = None
        trace_id = store.store_response_trace(trace_data)
        assert trace_id, "trace_id should be non-empty"
        with psycopg.connect(_config.database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT verdict, reasoning FROM runtime_response_traces WHERE id = %s::uuid;",
                    (trace_id,),
                )
                row = cur.fetchone()
        assert row, "trace row not found in DB"
        assert row[0] == "approved"
        assert row[1] == "trace-storage-test"
        _pass_test("evaluator: response trace stored in DB", f"trace={trace_id[:8]}")
    except Exception as exc:
        _fail_test("evaluator: response trace stored in DB", str(exc))
        traceback.print_exc()
    finally:
        _cleanup_trace(trace_id)


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    _p("\n=== test_response_evaluator ===\n")

    _p("-- Unit tests: hard override --")
    test_hard_override_refuse_returns_blocked()
    test_hard_override_ask_user_no_question_returns_needs_revision()
    test_hard_override_ask_user_with_question_returns_none()
    test_hard_override_normal_answer_returns_none()
    test_hard_override_answer_with_caveat_returns_none()

    _p("\n-- Unit tests: critic parser --")
    test_parse_critic_valid_json()
    test_parse_critic_unknown_verdict_normalised()
    test_parse_critic_unknown_overstatement_normalised()
    test_parse_critic_invalid_json_returns_fallback()
    test_parse_critic_strips_markdown_fences()
    test_parse_critic_commit_candidates_extracted()
    test_parse_critic_short_candidates_filtered()

    _p("\n-- Unit tests: deterministic verdict --")
    test_deterministic_verdict_sufficient_approved()
    test_deterministic_verdict_conflicting_needs_caveat()
    test_deterministic_verdict_low_confidence_needs_caveat()
    test_deterministic_verdict_ask_user_no_question()

    _p("\n-- Unit tests: dataclass --")
    test_response_evaluation_to_dict_has_required_keys()
    test_allowed_sets_complete()

    _p("\n-- Integration tests (StubLLM + real DB) --")
    test_evaluator_approved_answer_passes_through()
    test_evaluator_refuse_action_blocks_answer()
    test_evaluator_refuse_generates_polite_refusal()
    test_evaluator_ask_user_no_question_needs_revision()
    test_evaluator_verify_then_answer_runs_verification()
    test_evaluator_needs_revision_critic_no_revised_runs_revision_pass()
    test_evaluator_needs_caveat_verdict_preserved()
    test_evaluator_commit_candidates_extracted()
    test_evaluator_use_critic_false_deterministic()
    test_evaluator_response_trace_stored_in_db()

    _p(f"\nResults: {_pass} PASS  {_warn} WARN  {_fail} FAIL\n")
    if _fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
