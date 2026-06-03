#!/usr/bin/env python3
"""
Tests for the post-turn reflection system.

Covers:
  - _extract_thinking (Phases 1)
  - build_reflection_prompt / extract_candidates_from_turn (Phase 2)
  - _retrieve_with_fallback (Phase 5)
  - clean_assistant_response think-block stripping (Phase 1)
  - _post_turn_reflection integration (Phase 3)
  - config new fields (Phases 4+5)
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── test runner ─────────────────────────────────────────────────────────────
_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def _pass(name: str) -> None:
    _results.append(("PASS", name, ""))
    print(f"  PASS  {name}")


def _fail(name: str, detail: str) -> None:
    _results.append(("FAIL", name, detail))
    print(f"  FAIL  {name}: {detail}")


def _warn(name: str, detail: str) -> None:
    _results.append(("WARN", name, detail))
    print(f"  WARN  {name}: {detail}")


def _assert(cond: bool, name: str, detail: str = "") -> None:
    if cond:
        _pass(name)
    else:
        _fail(name, detail or "assertion failed")


# ── stubs ────────────────────────────────────────────────────────────────────

class StubLLM:
    """Returns configurable JSON responses for testing."""

    def __init__(self, response: str = ""):
        self._response = response

    def generate_response(self, prompt: str, system: str | None = None) -> str:
        return self._response

    def embed_text(self, text: str) -> list[float]:
        return [0.1] * 10


_STUB_ONE_CANDIDATE = json.dumps({
    "candidates": [{
        "content": "The user prefers PostgreSQL over SQLite for persistent storage.",
        "context_summary": "User prefers PostgreSQL over SQLite.",
        "memory_type": "preference",
        "scope": "user",
        "confidence": 0.9,
        "importance": 0.8,
        "should_store": True,
        "reason": "Clear durable user preference stated explicitly.",
    }]
})

_STUB_EMPTY = json.dumps({"candidates": []})

_STUB_FROM_REASONING = json.dumps({
    "candidates": [{
        "content": "Embedding dimensions for qwen3-embedding are 4096, which prevents HNSW index creation in pgvector.",
        "context_summary": "qwen3-embedding outputs 4096-dim vectors; pgvector HNSW doesn't support this.",
        "memory_type": "fact",
        "scope": "memory-layer-project",
        "confidence": 0.95,
        "importance": 0.85,
        "should_store": True,
        "reason": "Derived from reasoning; non-obvious and durably useful for future architecture decisions.",
    }]
})


# ── Phase 1: _extract_thinking ───────────────────────────────────────────────
print("\n-- Phase 1: _extract_thinking --")

from app.chat import _extract_thinking, _THINK_PATTERN

def test_extract_thinking_basic() -> None:
    raw = "<think>I need to check the config.</think>The answer is 42."
    answer, thinking = _extract_thinking(raw)
    _assert(thinking == "I need to check the config.", "extract_thinking: inner content captured")
    _assert(answer == "The answer is 42.", "extract_thinking: think block removed from answer")

def test_extract_thinking_no_blocks() -> None:
    raw = "Just a plain answer."
    answer, thinking = _extract_thinking(raw)
    _assert(thinking == "", "extract_thinking: no blocks → empty thinking")
    _assert(answer == "Just a plain answer.", "extract_thinking: no blocks → answer unchanged")

def test_extract_thinking_multiple_blocks() -> None:
    raw = "<think>Step 1: check X.</think>Partial.<think>Step 2: verify Y.</think>Final answer."
    answer, thinking = _extract_thinking(raw)
    _assert("Step 1: check X." in thinking, "extract_thinking: first block captured")
    _assert("Step 2: verify Y." in thinking, "extract_thinking: second block captured")
    _assert("<think>" not in answer, "extract_thinking: no think tags in answer")

def test_extract_thinking_case_insensitive() -> None:
    raw = "<THINK>reasoning here</THINK>result"
    answer, thinking = _extract_thinking(raw)
    _assert("reasoning here" in thinking, "extract_thinking: case-insensitive match")
    _assert(answer.strip() == "result", "extract_thinking: answer clean after case-insensitive strip")

def test_extract_thinking_multiline() -> None:
    raw = "<think>\nLine 1.\nLine 2.\n</think>\nFinal."
    answer, thinking = _extract_thinking(raw)
    _assert("Line 1." in thinking, "extract_thinking: multiline block captured")
    _assert("Final." in answer, "extract_thinking: answer correct after multiline strip")

test_extract_thinking_basic()
test_extract_thinking_no_blocks()
test_extract_thinking_multiple_blocks()
test_extract_thinking_case_insensitive()
test_extract_thinking_multiline()


# ── Phase 1: clean_assistant_response strips think blocks ────────────────────
print("\n-- Phase 1: clean_assistant_response think stripping --")

from app.chat import clean_assistant_response

def test_clean_strips_think() -> None:
    raw = "<think>internal reasoning</think>The actual answer."
    cleaned = clean_assistant_response(raw)
    _assert("<think>" not in cleaned, "clean_assistant_response: think tags removed")
    _assert("internal reasoning" not in cleaned, "clean_assistant_response: think content removed")
    _assert("The actual answer." in cleaned, "clean_assistant_response: answer preserved")

def test_clean_no_think_unchanged() -> None:
    raw = "Normal answer without think blocks."
    cleaned = clean_assistant_response(raw)
    _assert("Normal answer without think blocks." in cleaned,
            "clean_assistant_response: no-think answer preserved")

test_clean_strips_think()
test_clean_no_think_unchanged()


# ── Phase 2: build_reflection_prompt ─────────────────────────────────────────
print("\n-- Phase 2: build_reflection_prompt --")

from scripts.extract_memory_candidates import build_reflection_prompt, extract_candidates_from_turn

def test_reflection_prompt_includes_all_parts() -> None:
    prompt = build_reflection_prompt(
        user_msg="I use PostgreSQL.",
        thinking="The user clearly stated a preference for PostgreSQL.",
        answer="Noted. PostgreSQL will be used.",
    )
    _assert("User: I use PostgreSQL." in prompt, "build_reflection_prompt: user msg present")
    _assert("Assistant reasoning:" in prompt, "build_reflection_prompt: reasoning section present")
    _assert("Assistant answer:" in prompt, "build_reflection_prompt: answer section present")
    _assert("should_store" in prompt, "build_reflection_prompt: JSON schema hint present")

def test_reflection_prompt_no_thinking_section() -> None:
    prompt = build_reflection_prompt(
        user_msg="Hello.",
        thinking="",
        answer="Hi there.",
    )
    _assert("Assistant reasoning:" not in prompt,
            "build_reflection_prompt: reasoning section absent when no thinking")

def test_reflection_prompt_truncates_long_content() -> None:
    long_msg = "x" * 5000
    long_thinking = "y" * 5000
    long_answer = "z" * 5000
    prompt = build_reflection_prompt(long_msg, long_thinking, long_answer)
    # Limits: user 1000, thinking 2000, answer 1500 — prompt won't be 13500+ chars
    _assert(len(prompt) < 10000, "build_reflection_prompt: content truncated")

test_reflection_prompt_includes_all_parts()
test_reflection_prompt_no_thinking_section()
test_reflection_prompt_truncates_long_content()


# ── Phase 2: extract_candidates_from_turn ────────────────────────────────────
print("\n-- Phase 2: extract_candidates_from_turn (StubLLM) --")

import unittest.mock as mock

def test_extract_from_turn_returns_candidates() -> None:
    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=StubLLM(_STUB_ONE_CANDIDATE)):
        result = extract_candidates_from_turn(
            user_msg="I prefer PostgreSQL.",
            thinking="",
            answer="Understood, PostgreSQL it is.",
        )
    candidates = result.get("candidates", [])
    _assert(len(candidates) == 1, "extract_from_turn: one candidate returned")
    _assert(candidates[0].get("memory_type") == "preference", "extract_from_turn: memory_type preserved")

def test_extract_from_turn_empty_result() -> None:
    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=StubLLM(_STUB_EMPTY)):
        result = extract_candidates_from_turn(
            user_msg="What time is it?",
            thinking="",
            answer="I don't have access to real-time data.",
        )
    _assert(result == {"candidates": []}, "extract_from_turn: empty candidates for trivial Q&A")

def test_extract_from_turn_reasoning_derived() -> None:
    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=StubLLM(_STUB_FROM_REASONING)):
        result = extract_candidates_from_turn(
            user_msg="Why can't we add an HNSW index?",
            thinking="The embedding dimensions are 4096, pgvector HNSW only supports up to 2000.",
            answer="HNSW is unsupported at 4096 dims.",
        )
    candidates = result.get("candidates", [])
    _assert(len(candidates) == 1, "extract_from_turn: reasoning-derived candidate present")
    _assert("4096" in candidates[0].get("content", ""), "extract_from_turn: reasoning content captured")

def test_extract_from_turn_raises_on_empty_input() -> None:
    try:
        extract_candidates_from_turn("", "", "")
        _fail("extract_from_turn: empty input raises", "no exception raised")
    except ValueError:
        _pass("extract_from_turn: empty input raises ValueError")

def test_extract_from_turn_include_rejected() -> None:
    stub_with_rejected = json.dumps({"candidates": [
        {"content": "A.", "memory_type": "fact", "scope": None, "confidence": 0.8,
         "importance": 0.5, "should_store": False, "reason": "trivial", "context_summary": "A."},
    ]})
    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=StubLLM(stub_with_rejected)):
        result_excluded = extract_candidates_from_turn("msg", "", "ans", include_rejected=False)
        result_included = extract_candidates_from_turn("msg", "", "ans", include_rejected=True)

    _assert(len(result_excluded["candidates"]) == 0, "extract_from_turn: rejected excluded by default")
    _assert(len(result_included["candidates"]) == 1, "extract_from_turn: rejected included when requested")

test_extract_from_turn_returns_candidates()
test_extract_from_turn_empty_result()
test_extract_from_turn_reasoning_derived()
test_extract_from_turn_raises_on_empty_input()
test_extract_from_turn_include_rejected()


# ── Phase 5: _retrieve_with_fallback ─────────────────────────────────────────
print("\n-- Phase 5: _retrieve_with_fallback --")

from app.chat import _retrieve_with_fallback
from app.memory_store import MemoryStore

def test_fallback_returns_first_nonempty() -> None:
    """With adaptive enabled, should return on the first threshold that yields atoms."""
    call_log: list[float] = []

    def fake_retrieve(query: str, limit: int = 5, min_similarity: float | None = None, scope_filter: str | None = None, scope_filters: list | None = None) -> list[dict]:
        call_log.append(min_similarity or 0.0)
        if (min_similarity or 0.0) <= 0.40:
            return [{"id": "abc", "content": "something"}]
        return []

    store = mock.MagicMock(spec=MemoryStore)
    store.retrieve_memories.side_effect = fake_retrieve

    result = _retrieve_with_fallback("test query", store, limit=5)

    _assert(len(result) == 1, "retrieve_with_fallback: returns atoms found at relaxed threshold")
    _assert(len(call_log) == 2, f"retrieve_with_fallback: stopped after 2 attempts, got {len(call_log)}")

def test_fallback_returns_empty_when_all_fail() -> None:
    store = mock.MagicMock(spec=MemoryStore)
    store.retrieve_memories.return_value = []
    result = _retrieve_with_fallback("nothing matches", store, limit=5)
    _assert(result == [], "retrieve_with_fallback: empty list when all thresholds exhausted")

def test_fallback_skips_cascade_when_disabled() -> None:
    store = mock.MagicMock(spec=MemoryStore)
    store.retrieve_memories.return_value = []

    from app.config import AppConfig, get_config
    original = get_config()
    disabled_cfg = AppConfig(
        database_url=original.database_url,
        ollama_host=original.ollama_host,
        chat_model=original.chat_model,
        embedding_model=original.embedding_model,
        openai_compat_base_url=original.openai_compat_base_url,
        openai_compat_api_key=original.openai_compat_api_key,
        memory_retrieval_threshold=original.memory_retrieval_threshold,
        web_research_enabled=original.web_research_enabled,
        web_search_provider=original.web_search_provider,
        web_search_api_key=original.web_search_api_key,
        history_window_size=original.history_window_size,
        adaptive_retrieval_enabled=False,
        adaptive_retrieval_thresholds=original.adaptive_retrieval_thresholds,
    )
    with mock.patch("app.chat.get_config", return_value=disabled_cfg):
        _retrieve_with_fallback("query", store, limit=5)

    _assert(store.retrieve_memories.call_count == 1,
            "retrieve_with_fallback: only one call when adaptive disabled")

test_fallback_returns_first_nonempty()
test_fallback_returns_empty_when_all_fail()
test_fallback_skips_cascade_when_disabled()


# ── Phase 4+5: config new fields ─────────────────────────────────────────────
print("\n-- Config: new fields --")

from app.config import get_config as real_get_config

def test_config_new_fields_exist() -> None:
    cfg = real_get_config()
    _assert(hasattr(cfg, "history_window_size"), "config: history_window_size field present")
    _assert(hasattr(cfg, "adaptive_retrieval_enabled"), "config: adaptive_retrieval_enabled field present")
    _assert(hasattr(cfg, "adaptive_retrieval_thresholds"), "config: adaptive_retrieval_thresholds field present")

def test_config_defaults() -> None:
    cfg = real_get_config()
    _assert(cfg.history_window_size == 6, f"config: history_window_size default 6, got {cfg.history_window_size}")
    _assert(cfg.adaptive_retrieval_enabled is True, "config: adaptive_retrieval_enabled default True")
    _assert(isinstance(cfg.adaptive_retrieval_thresholds, tuple), "config: thresholds is a tuple")
    _assert(len(cfg.adaptive_retrieval_thresholds) >= 2, "config: at least 2 thresholds defined")

test_config_new_fields_exist()
test_config_defaults()


# ── Phase 3: _post_turn_reflection (integration with StubLLM) ────────────────
print("\n-- Phase 3: _post_turn_reflection (StubLLM + real DB) --")

from app.chat import _post_turn_reflection

def test_post_turn_reflection_runs_without_error() -> None:
    """Reflection should be non-fatal even when extraction returns empty."""
    llm = StubLLM(_STUB_EMPTY)
    store = MemoryStore(ollama_client=llm)

    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=llm):
        try:
            _post_turn_reflection(
                user_msg="Hello.",
                thinking="",
                answer="Hi there.",
                llm=llm,
                memory_store=store,
            )
            _pass("post_turn_reflection: completes without exception on empty extraction")
        except Exception as exc:
            _fail("post_turn_reflection: completes without exception on empty extraction", str(exc))

def test_post_turn_reflection_stores_candidate() -> None:
    """With a storable candidate, the commit pipeline should attempt to store it."""
    llm = StubLLM(_STUB_ONE_CANDIDATE)
    store = MemoryStore(ollama_client=llm)

    commit_calls: list[dict] = []

    class TrackingPipeline:
        def commit_candidate(self, candidate: dict) -> None:
            commit_calls.append(candidate)

    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=llm), \
         mock.patch("app.commit_pipeline.MemoryCommitPipeline", return_value=TrackingPipeline()):
        try:
            _post_turn_reflection(
                user_msg="I use PostgreSQL.",
                thinking="",
                answer="PostgreSQL noted.",
                llm=llm,
                memory_store=store,
            )
            _assert(len(commit_calls) == 1, "post_turn_reflection: one candidate committed")
            _assert("PostgreSQL" in commit_calls[0].get("content", ""),
                    "post_turn_reflection: committed candidate has expected content")
        except Exception as exc:
            _fail("post_turn_reflection: stores candidate", str(exc))

def test_post_turn_reflection_handles_extraction_failure() -> None:
    """If extraction raises, reflection should return silently."""
    llm = StubLLM("invalid json {{{{")
    store = MemoryStore(ollama_client=llm)

    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=llm):
        try:
            _post_turn_reflection("msg", "", "ans", llm, store)
            _pass("post_turn_reflection: handles extraction failure silently")
        except Exception as exc:
            _fail("post_turn_reflection: handles extraction failure silently", str(exc))

def test_post_turn_reflection_skips_rejected_candidates() -> None:
    """Candidates with should_store=False must not reach the pipeline."""
    stub_rejected = json.dumps({"candidates": [
        {"content": "Some trivial thing.", "memory_type": "fact", "scope": None,
         "confidence": 0.5, "importance": 0.2, "should_store": False,
         "reason": "not durable", "context_summary": "trivial"},
    ]})
    llm = StubLLM(stub_rejected)
    store = MemoryStore(ollama_client=llm)

    commit_calls: list[dict] = []

    class TrackingPipeline:
        def commit_candidate(self, candidate: dict) -> None:
            commit_calls.append(candidate)

    with mock.patch("scripts.extract_memory_candidates.get_llm_client", return_value=llm), \
         mock.patch("app.commit_pipeline.MemoryCommitPipeline", return_value=TrackingPipeline()):
        _post_turn_reflection("msg", "", "ans", llm, store)

    _assert(len(commit_calls) == 0, "post_turn_reflection: rejected candidates not committed")

test_post_turn_reflection_runs_without_error()
test_post_turn_reflection_stores_candidate()
test_post_turn_reflection_handles_extraction_failure()
test_post_turn_reflection_skips_rejected_candidates()


# ── summary ───────────────────────────────────────────────────────────────────
print()
passed = sum(1 for s, _, _ in _results if s == "PASS")
warned = sum(1 for s, _, _ in _results if s == "WARN")
failed = sum(1 for s, _, _ in _results if s == "FAIL")
print(f"Results: {passed} PASS  {warned} WARN  {failed} FAIL")
if failed:
    sys.exit(1)
